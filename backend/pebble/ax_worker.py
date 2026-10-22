"""Accessibility (AX) worker for scoped screen sessions (Phase 16, D04).

Two halves share this module so the frozen executable needs no extra file:

worker   `pebble-backend --ax-worker` reads one JSON request per line on stdin
         and answers one JSON line on stdout. Every answer passes through the
         pure sanitizers below before it is written, so secure-field values
         never leave the worker and every string is bounded.
client   WorkerClient spawns the worker lazily, frames requests, enforces a
         per-request deadline and kills the worker on any protocol failure. A
         perform whose answer never arrives raises AXError('uncertain'); it is
         never retried.

The PID in a request is a transient lookup the host re-resolved for this one
call from the bundle ID plus the process start time. Backends must refuse
(`instance_changed`) when the PID's start time no longer equals `start`, so a
reused PID can never be driven (handoff contract 10).

Real Accessibility is blocked on the person's grant and experiment E-N2 (is
TCC attributed to Pebble.app when a frozen backend child calls AX?). Until
that evidence exists the only production backend is BlockedBackend, which
refuses every call without touching any OS API. Tests drive the protocol with
a fake backend. Nothing here captures the screen, launches apps, synthesizes
keyboard events or reads real windows.
"""

import hashlib
import json
import math
import os
import re
import selectors
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

from .text_safety import INVISIBLE as SHARED_INVISIBLE, UNSAFE, secret_text


PROTOCOL = 'pebble-ax/1'
OPS = ('hello', 'windows', 'tree', 'perform')
ACTIONS = ('focus', 'scroll', 'expand', 'collapse', 'select', 'toggle', 'type', 'press', 'enter')
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 1_048_576
REQUEST_SECONDS = 5.0
MAX_WINDOWS = 32
MAX_ELEMENTS = 300
MAX_LABEL_SOURCE = 512      # each label source is clipped here before hashing, in the worker and the host alike
MAX_TITLE_SOURCE = 1024
MAX_VALUE = 200
MAX_PATH = 512
MAX_REF = 128
MAX_URL = 2048
MAX_TEXT = 4000
ROLE = re.compile(r'AX[A-Za-z]{1,48}')
HEX64 = re.compile(r'[0-9a-f]{64}')
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}')
LABEL_KEYS = ('title', 'description', 'placeholder', 'help')
FLAGS = ('enabled', 'focused', 'selected', 'expanded', 'editable', 'multiline', 'download')
SECURE_ROLES = frozenset({'AXSecureTextField'})
TEXT_ROLES = frozenset({'AXTextField', 'AXTextArea', 'AXComboBox', 'AXSearchField', 'AXSecureTextField'})
SECURE_AUTOCOMPLETE = re.compile(r'(?:^|\s)(one-time-code|cc-[a-z-]+|current-password|new-password)(?:\s|$)')
# A label can only raise a field to secure, never lower one: the role, subrole,
# HTML type and autocomplete decide first.
SECRET_LABEL = re.compile(
    r'\b(pass[\s-]?(?:word|code|phrase)s?|pins?|one[- ]?time|otp|2fa|mfa|two[- ](?:factor|step)|'
    r'(?:verification|security|authentication|auth|login|sign[- ]in|access|confirmation) code|'
    r'cvv|cvc|csc|card number|credit card|secret|private key|api key|recovery (?:code|phrase|key)|'
    r'seed phrase|ssn|social security)\b', re.I)
# Phase 24 (V1-054, V1-168): one shared set, written as escapes (text_safety). It now also covers the bidi
# isolates U+2066-U+2069, U+061C, U+180E and the tag block, which sources.INVISIBLE already stripped.
CONTROL = UNSAFE
# Invisible format characters are removed (not spaced) so they cannot split a
# word like "Pass" + U+202E + "code" past the secret-label check; controls become spaces.
INVISIBLE = SHARED_INVISIBLE
# Window facts an adapter may report. 'instance' is added by the host adapter
# (bundle ID @ process start), never by the worker.
WINDOW_TEXT = {'window_ref': MAX_REF, 'ax_identifier': 200, 'tab_id': MAX_REF, 'origin': 300, 'url': MAX_URL,
               'navigation_id': MAX_REF, 'loader_id': MAX_REF, 'instance': 300}
WINDOW_PATH = '.'   # perform(path='.', action='focus') raises the window itself


class AXError(Exception):
    """A refused or failed AX call. code is one of CODES; detail is short, content-free text."""

    CODES = ('unavailable', 'not_trusted', 'not_found', 'instance_changed', 'unsupported', 'invalid', 'failed',
             'timeout', 'uncertain', 'protocol')

    def __init__(self, code, detail=''):
        super().__init__(detail or code)
        self.code = code if code in self.CODES else 'failed'
        self.detail = ' '.join(str(detail).split())[:300]


def sha256(text):
    return hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()


def clip(value, limit, *, lower=False):
    """Printable NFC text clipped to limit characters, or None for non-text."""
    if not isinstance(value, str):
        return None
    text = CONTROL.sub(' ', INVISIBLE.sub('', unicodedata.normalize('NFC', value)))[:limit]
    return text.lower() if lower else text


def _frame(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e6 for v in value):
        return None
    return [int(round(v)) for v in value]


def editable_text(element):
    return element.get('role') in TEXT_ROLES or element.get('editable') is True


def label_sources(element):
    """The texts a label hash covers. Static text carries its words in the value."""
    sources = [element.get(k) for k in LABEL_KEYS]
    if element.get('role') == 'AXStaticText' and isinstance(element.get('value'), str):
        sources.append(element['value'])
    return sources


def label_text(element):
    """The words a person would read for this element: title, description, static text, then hints."""
    static = element.get('value') if element.get('role') == 'AXStaticText' else None
    for text in (element.get('title'), element.get('description'), static, element.get('placeholder'),
                 element.get('help')):
        if isinstance(text, str) and text.strip():
            return ' '.join(text.split())
    return ''


def label_sha256(element):
    return sha256(json.dumps(label_sources(element), ensure_ascii=False, separators=(',', ':')))


def is_secure(element):
    """True for a field whose value Pebble must never read or type: role, HTML type,
    autocomplete, or (for editable text only) a secret-looking label."""
    if element.get('secure') is True:
        return True
    if element.get('role') in SECURE_ROLES or element.get('subrole') in SECURE_ROLES:
        return True
    if element.get('html_type') == 'password':
        return True
    if isinstance(element.get('autocomplete'), str) and SECURE_AUTOCOMPLETE.search(element['autocomplete']):
        return True
    # Phase 24 (V1-054): the label is folded first (secret_text), so no invisible, format or combining character
    # and no compatibility form can split or disguise a secret word. Raise-only: it never lowers a field.
    return editable_text(element) and any(
        isinstance(t, str) and SECRET_LABEL.search(secret_text(t)) for t in [element.get(k) for k in LABEL_KEYS])


def sanitize_element(raw):
    """A bounded, redacted copy of one adapter element, or None when malformed.

    Idempotent: sanitizing an already sanitized element returns an equal dict,
    so the host can re-apply it to worker output as defence in depth.
    """
    if not isinstance(raw, dict):
        return None
    role, path = raw.get('role'), raw.get('path')
    if not isinstance(role, str) or not ROLE.fullmatch(role):
        return None
    if not isinstance(path, str) or not 1 <= len(path) <= MAX_PATH or CONTROL.search(path):
        return None
    subrole = raw.get('subrole')
    out = {'role': role, 'subrole': subrole if isinstance(subrole, str) and ROLE.fullmatch(subrole) else None,
           'path': path}
    for key in LABEL_KEYS:
        out[key] = clip(raw.get(key), MAX_LABEL_SOURCE)
    for key in FLAGS:
        out[key] = raw[key] if type(raw.get(key)) is bool else None
    out['frame'] = _frame(raw.get('frame'))
    out['html_type'] = clip(raw.get('html_type'), 32, lower=True)
    out['autocomplete'] = clip(raw.get('autocomplete'), 64, lower=True)
    out['href'] = clip(raw.get('href'), MAX_URL)
    value = raw.get('value')
    out['value'] = None
    if out['role'] == 'AXStaticText' and isinstance(value, str):
        out['value'] = clip(value, MAX_LABEL_SOURCE)  # a label source, hashed in full like the others
    out['secure'] = bool(raw.get('secure') is True or is_secure({**out, 'secure': None}))
    out['value_sha256'] = out['value_truncated'] = None
    if out['secure']:
        out['value'] = None
    elif out['role'] != 'AXStaticText':
        if type(value) is bool or type(value) is int and abs(value) < 1 << 53:
            out['value'] = value
        elif type(value) is float and math.isfinite(value):
            out['value'] = value
        elif isinstance(value, str):
            text = clip(value, MAX_TEXT * 4)
            kept = raw.get('value_sha256') if raw.get('value_truncated') is True else None
            out['value'] = text[:MAX_VALUE]
            out['value_truncated'] = raw.get('value_truncated') is True or len(text) > MAX_VALUE
            out['value_sha256'] = kept if isinstance(kept, str) and HEX64.fullmatch(kept) else sha256(text)
    return out


def value_digest(text):
    """The value_sha256 an element reports once its value equals text."""
    return sha256(clip(text, MAX_TEXT * 4))


def sanitize_window(raw):
    if not isinstance(raw, dict):
        return None
    out = {'title': clip(raw.get('title'), MAX_TITLE_SOURCE) or '', 'focused': raw.get('focused') is True,
           'frame': _frame(raw.get('frame'))}
    for key, limit in WINDOW_TEXT.items():
        text = clip(raw.get(key), limit)
        out[key] = text if text else None
    out['title_sha256'] = sha256(out['title'])
    return out


def sanitize_snapshot(raw, limit=MAX_ELEMENTS):
    """{'window', 'elements', 'truncated', 'dropped'} with at most limit elements."""
    if not isinstance(raw, dict):
        raise AXError('protocol', 'The snapshot is not an object.')
    window = sanitize_window(raw.get('window'))
    if window is None:
        raise AXError('protocol', 'The snapshot has no window.')
    items = raw.get('elements')
    if not isinstance(items, list):
        raise AXError('protocol', 'The snapshot has no element list.')
    prior = raw.get('dropped')
    elements, dropped = [], prior if type(prior) is int and 0 <= prior <= 1 << 20 else 0
    for item in items[:limit]:
        element = sanitize_element(item)
        if element is None:
            dropped += 1
        else:
            elements.append(element)
    return {'window': window, 'elements': elements,
            'truncated': raw.get('truncated') is True or len(items) > limit, 'dropped': dropped}


# ---- worker -----------------------------------------------------------------

class BlockedBackend:
    """The production backend until E-N2: refuses every call, touches no OS API."""

    name = 'blocked'
    available = False
    REASON = ('Accessibility control is not enabled yet: it waits for the Accessibility grant and the E-N2 '
              'check that macOS attributes it to Pebble.')

    def windows(self, pid, start):
        raise AXError('unavailable', self.REASON)

    def tree(self, pid, start, window_ref, limit):
        raise AXError('unavailable', self.REASON)

    def perform(self, pid, start, window_ref, path, action, text):
        raise AXError('unavailable', self.REASON)


def _instance_args(args):
    pid, start = args.get('pid'), args.get('start')
    if type(pid) is not int or not 0 < pid < 1 << 31:
        raise AXError('invalid', 'pid')
    if type(start) not in (int, float) or not math.isfinite(start) or start <= 0:
        raise AXError('invalid', 'start')
    return pid, float(start)


def _ref(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not REF.fullmatch(value):
        raise AXError('invalid', key)
    return value


def handle(request, backend):
    """One decoded request -> one response object. Never raises."""
    rid = request.get('id') if isinstance(request, dict) else None
    rid = rid if type(rid) is int and 0 <= rid < 1 << 53 else None
    try:
        if rid is None or set(request) != {'id', 'op', 'args'} or request['op'] not in OPS \
                or not isinstance(request['args'], dict):
            raise AXError('invalid', 'request')
        op, args = request['op'], request['args']
        if op == 'hello':
            result = {'protocol': PROTOCOL, 'backend': getattr(backend, 'name', 'unknown'),
                      'available': getattr(backend, 'available', True) is True}
        elif op == 'windows':
            pid, start = _instance_args(args)
            rows = backend.windows(pid, start)
            if not isinstance(rows, list):
                raise AXError('protocol', 'windows')
            result = [w for w in (sanitize_window(r) for r in rows[:MAX_WINDOWS]) if w is not None]
        elif op == 'tree':
            pid, start = _instance_args(args)
            limit = args.get('limit')
            if type(limit) is not int or not 1 <= limit <= MAX_ELEMENTS:
                raise AXError('invalid', 'limit')
            result = sanitize_snapshot(backend.tree(pid, start, _ref(args, 'window_ref'), limit), limit)
        else:
            pid, start = _instance_args(args)
            path, action, text = args.get('path'), args.get('action'), args.get('text')
            if not isinstance(path, str) or not 1 <= len(path) <= MAX_PATH or CONTROL.search(path):
                raise AXError('invalid', 'path')
            if action not in ACTIONS:
                raise AXError('invalid', 'action')
            if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT or '\x00' in text):
                raise AXError('invalid', 'text')
            backend.perform(pid, start, _ref(args, 'window_ref'), path, action, text)
            result = {'performed': True}
        return {'id': rid, 'ok': True, 'result': result}
    except AXError as exc:
        return {'id': rid, 'ok': False, 'error': exc.code, 'detail': exc.detail}
    except Exception as exc:  # backend bugs answer content-free; the worker keeps serving
        return {'id': rid, 'ok': False, 'error': 'failed', 'detail': type(exc).__name__}


def _encode(response):
    data = json.dumps(response, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
    if len(data) > MAX_RESPONSE_BYTES:
        data = json.dumps({'id': response.get('id'), 'ok': False, 'error': 'protocol',
                           'detail': 'The answer exceeded its size limit.'}).encode('utf-8') + b'\n'
    return data


def serve(rfile, wfile, backend):
    """Answer requests until EOF. An oversized or undecodable line ends the session."""
    while True:
        line = rfile.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_REQUEST_BYTES or not line.endswith(b'\n'):
            wfile.write(_encode({'id': None, 'ok': False, 'error': 'protocol', 'detail': 'request too large'}))
            wfile.flush()
            return
        try:
            request = json.loads(line)
        except ValueError:
            wfile.write(_encode({'id': None, 'ok': False, 'error': 'protocol', 'detail': 'request is not JSON'}))
            wfile.flush()
            return
        wfile.write(_encode(handle(request, backend)))
        wfile.flush()


def worker_main():
    """`--ax-worker` entry point. Serves BlockedBackend until E-N2 adds the real adapter."""
    import resource
    for limit, value in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_NOFILE, 64),
                         (resource.RLIMIT_CPU, 120)):
        resource.setrlimit(limit, (value, value))
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # libraries must never corrupt the protocol
    serve(sys.stdin.buffer, out, BlockedBackend())


# ---- host client ------------------------------------------------------------

def worker_argv():
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--ax-worker']
    return [sys.executable, '-s', str(Path(__file__).resolve().parents[1] / 'main.py'), '--ax-worker']


def _spawn(argv):
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            close_fds=True, start_new_session=True,
                            env={'PATH': '/usr/bin:/bin', 'TMPDIR': os.environ.get('TMPDIR', '/tmp')})


class WorkerClient:
    """Host side of the protocol. One request at a time; any failure kills the worker.

    spawn(argv) must return an object with stdin, stdout (with fileno), poll(),
    kill() and wait(); tests pass a pipe pair served by an in-process thread.
    """

    name = 'worker'
    available = True

    def __init__(self, argv=None, *, timeout=REQUEST_SECONDS, spawn=None, clock=time.monotonic):
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError('Choose a request timeout from above 0 to 60 seconds.')
        self.argv, self.timeout = list(argv or worker_argv()), float(timeout)
        self.spawn, self.clock = spawn or _spawn, clock
        self.proc, self.buffer, self.next_id = None, b'', 0
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            self._kill()

    def _kill(self):
        proc, self.proc, self.buffer = self.proc, None, b''
        if proc is None:
            return
        for stream in (getattr(proc, 'stdin', None), getattr(proc, 'stdout', None)):
            try:
                stream.close()
            except Exception:
                pass
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass

    def _readline(self, deadline):
        fd = self.proc.stdout.fileno()
        # Not select(): it refuses descriptors numbered 1024 (FD_SETSIZE) or above, which a busy host reaches.
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise AXError('timeout', 'The accessibility worker did not answer in time.')
                if not selector.select(remaining):
                    continue
                chunk = os.read(fd, 65_536)
                if not chunk:
                    raise AXError('protocol', 'The accessibility worker exited.')
                self.buffer += chunk
                if len(self.buffer) > MAX_RESPONSE_BYTES + 1:
                    raise AXError('protocol', 'The accessibility worker answer exceeded its size limit.')
        line, _, self.buffer = self.buffer.partition(b'\n')
        return line

    def call(self, op, args, *, timeout=None):
        if op not in OPS:
            raise AXError('invalid', 'op')
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            data = json.dumps({'id': rid, 'op': op, 'args': args}, ensure_ascii=False,
                              separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
            if len(data) > MAX_REQUEST_BYTES:
                raise AXError('invalid', 'The request exceeded its size limit.')
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._kill()
                    self.proc = self.spawn(self.argv)
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
                line = self._readline(self.clock() + (self.timeout if timeout is None else timeout))
                response = json.loads(line)
                if not isinstance(response, dict) or response.get('id') != rid or type(response.get('ok')) is not bool:
                    raise AXError('protocol', 'The accessibility worker answered out of order.')
            except (AXError, OSError, ValueError) as exc:
                self._kill()
                if op == 'perform':  # the act may or may not have happened; never retry it
                    raise AXError('uncertain', getattr(exc, 'detail', type(exc).__name__)) from None
                if isinstance(exc, AXError):
                    raise
                raise AXError('protocol', type(exc).__name__) from None
        if not response['ok']:
            raise AXError(response.get('error'), response.get('detail') or '')
        return response.get('result')

    def windows(self, pid, start):
        return self.call('windows', {'pid': pid, 'start': start})

    def tree(self, pid, start, window_ref, limit):
        return self.call('tree', {'pid': pid, 'start': start, 'window_ref': window_ref, 'limit': limit})

    def perform(self, pid, start, window_ref, path, action, text):
        return self.call('perform', {'pid': pid, 'start': start, 'window_ref': window_ref, 'path': path,
                                     'action': action, 'text': text})
