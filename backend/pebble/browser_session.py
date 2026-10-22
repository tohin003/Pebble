"""Pebble's own browser for scoped screen sessions over a CDP pipe (Phase 16, D04).

A Chromium-family browser runs with `--remote-debugging-pipe` and a private
`--user-data-dir` under Pebble's home. It never gets a TCP debugging port:
commands go in on its fd 3 and answers come back on its fd 4, as JSON messages
each followed by a NUL byte. BrowserAdapter implements the screen_session
adapter protocol (pin / snapshot / perform / windows / focus_window) for
`{kind:'browser', profile:'pebble', tab_id, origins}` targets, so ScreenSessions
owns grants, freshness, classes, review, handoff and readback exactly as for
native windows. This module adds what only a browser can check:

scope      One tab of one browser instance. The pin binds the launch instance
           and tab ID. Only a tab the live browser reported closed is
           `target_gone`; a dead pipe or a restarted browser ends the session
           (`session_expired`) and a crashed tab is `ax_failed`, so a crash is
           never read back as a window that closed.
           A page outside the granted origins is never read: the snapshot
           reports only the tab facts, and the host hands off (cross_origin).
           Frames from other origins are never attached to or read. Each
           visible one is a single opaque placeholder; Pebble refuses to act
           inside it, and one that looks like sign-in, payment or a credential
           API (`allow=`) is marked secure, which hands off.
identity   Origin, URL, history entry and loader ID come from the browser,
           never from page text, so a spoofed title cannot move identity or
           scope. navigation_id is `<history entry>.<n>` where n counts the
           main-frame navigation requests and guard-blocked requests Pebble saw.
re-check   Immediately before dispatch the adapter re-resolves the element by
           its backend node ID (embedded in the path), compares role, label
           hash and secure flag, requires its box within the frame tolerance,
           hit-tests the click point (an overlay or another frame refuses),
           refuses if the tab navigated since the host's fresh snapshot, and
           stops typing if the page moved the keyboard focus elsewhere.
guard      Local and input acts run inside an effect guard: requests are
           paused for the act and a settle window, and main-frame document
           loads and non-GET/HEAD/OPTIONS requests are aborted. A blocked request
           changes navigation_id, so the host reads the act back as unknown and
           asks for reconciliation instead of letting an auto-submitting form
           send. A popup the page opens counts the same way (its own requests
           are not guarded). Reviewed commits run without the guard.

Input is trusted CDP input (mouse, key, insertText) or a small isolated-world
function; nothing runs in the page's own JavaScript world. Downloads are
denied and file choosers are intercepted, so nothing is downloaded or
uploaded. Effects are never retried: a call that fails after an effect was
sent raises AXError('uncertain').

Until experiment E-B1 verifies a real browser over the pipe, the default
launcher refuses before creating the profile or spawning anything, and
PipeLauncher refuses every bundle without E-B1 evidence in VERIFIED_BROWSERS.
scripts/experiment_eb1.py gathers that evidence (the parent runs it; the
criteria are in docs/HARNESS_BROWSER_EXTENSION.md §7). Tests drive everything
through a fake pipe peer.

Phase 22: browser_bridge.ChromeAdapter reuses BrowserAdapter for tabs the
person shares from their own Chrome (Pebble for Chrome, over chrome.debugger).
"""

import fcntl
import hashlib
import json
import os
import plistlib
import re
import select
import shutil
import signal
import stat
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from . import ax_worker
from .ax_worker import AXError, MAX_URL, label_sha256, sanitize_element
from .policy import PolicyError
from .screen_session import LOGIN_ACTION, TAB_ID, TITLE_SHOWN, TWO_FACTOR, ScreenRefused, classify


CALL_SECONDS = 5.0
SETTLE_SECONDS = 0.5             # the effect guard stays armed this long after a local or input act
NAVIGATION_WAIT = 2.0            # a snapshot waits this long for a pending main-frame navigation
NAVIGATION_POLL = 0.1
READ_ATTEMPTS = 3
FRAME_TOLERANCE = 8              # px, as screen_session.FRAME_TOLERANCE
MAX_MESSAGE = 32 * 1024 * 1024   # one CDP message; larger ends the connection
MAX_FRAMES = 16
MAX_DOCUMENTS = 32
MAX_DOM_NODES = 100_000
MAX_AX_NODES = 20_000
MAX_TABS = 64
MAX_BLOCKED = 32
MIN_FRAME_AREA = 16              # px²; smaller embedded frames are not shown
PATH_STEPS = 320
CHAIN_KEEP = 16
WORLD = 'pebble-screen'
OBJECT_GROUP = 'pebble-act'
SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')
PROFILE_MARKER = '.pebble-browser-profile'
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
# Reviewed Chromium-family bundle IDs that support --remote-debugging-pipe.
CHROMIUM_BUNDLES = frozenset({
    'com.google.Chrome', 'com.google.Chrome.beta', 'com.google.Chrome.dev', 'com.google.Chrome.canary',
    'org.chromium.Chromium', 'com.microsoft.edgemac', 'com.microsoft.edgemac.Beta', 'com.microsoft.edgemac.Dev',
    'com.microsoft.edgemac.Canary', 'com.brave.Browser', 'com.brave.Browser.beta', 'com.brave.Browser.nightly'})
# bundle ID -> the parent's E-B1 evidence record. Empty until E-B1 runs, so nothing launches. It stays empty in code
# (validate_phase16 pins that): a passing run is recorded as a host-owned evidence file instead (load_verified).
VERIFIED_BROWSERS = {}
EB1_DIR, EB1_RECORD = 'e-b1', 'verified.json'      # under <home>/browser, beside (never inside) the profile
EB1_FORMAT, EB1_RECORD_FORMAT = 'pebble.experiment-eb1/1', 'pebble.verified-browser/1'
EB1_CRITERIA = ('bundle', 'argv', 'handshake', 'no_port', 'navigation', 'snapshot', 'input', 'secure', 'exit', 'profile')
EB1_FILE = re.compile(r'eb1-[0-9]{8}T[0-9]{6}Z\.json')
MAX_EVIDENCE = 1_048_576
FORBIDDEN_FLAGS = ('--remote-debugging-port', '--remote-debugging-address', '--remote-debugging-socket-name',
                   '--remote-allow-origins')
LAUNCH_FLAGS = ('--no-first-run', '--no-default-browser-check', '--disable-sync', '--disable-extensions',
                '--disable-default-apps')
UNAVAILABLE = ("Pebble's browser is not enabled yet: it waits for the E-B1 check that a Chromium-family browser "
               'runs over --remote-debugging-pipe with a fresh private profile and no TCP port.')
DIALOG = 'A page dialog is waiting for the person; Pebble does not answer page dialogs.'
EMBEDDED = 'Pebble does not read or act inside content embedded from another site.'
RESTARTED = "Pebble's browser stopped or restarted, so this tab session cannot continue."
CRASHED = 'The tab crashed or was detached, so Pebble cannot tell what happened in it.'

# Isolated-world functions (the page cannot redefine their built-ins).
VISIBILITY_JS = 'document.visibilityState'
CONTAINS_JS = ('function(o){return this===o||(this.nodeType===1&&this.contains(o))||'
               '(this.nodeType===3&&this.parentElement===o)}')
SELECT_ALL_JS = ('function(){this.focus();if(typeof this.select==="function"){this.select();return true}'
                 'const r=document.createRange();r.selectNodeContents(this);const s=getSelection();'
                 's.removeAllRanges();s.addRange(r);return true}')
CHOOSE_JS = ('function(t){if(this.tagName!=="SELECT")return false;const w=String(t).trim();'
             'for(const o of this.options){if(o.label.trim()===w||o.text.trim()===w){this.value=o.value;'
             'this.dispatchEvent(new Event("input",{bubbles:true}));'
             'this.dispatchEvent(new Event("change",{bubbles:true}));return true}}return false}')
OPTION_JS = ('function(){const s=this.closest("select");if(!s)return false;this.selected=true;'
             's.dispatchEvent(new Event("input",{bubbles:true}));'
             's.dispatchEvent(new Event("change",{bubbles:true}));return true}')
DETAILS_JS = ('function(o){const d=this.tagName==="SUMMARY"?this.parentElement:this;'
              'if(!d||d.tagName!=="DETAILS")return false;d.open=o;return true}')
FOCUSED_JS = 'function(){return this.ownerDocument.activeElement===this}'
ENTER = {'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13, 'text': '\r'}
BACKSPACE = {'key': 'Backspace', 'code': 'Backspace', 'windowsVirtualKeyCode': 8, 'nativeVirtualKeyCode': 51}

# Chromium AX roles -> the AX roles screen_session classifies. Page-authored
# roles never lower a class there: a browser press is always a commit.
ROLES = {
    'button': 'AXButton', 'link': 'AXLink', 'searchbox': 'AXSearchField', 'combobox': 'AXComboBox',
    'checkbox': 'AXCheckBox', 'menuitemcheckbox': 'AXCheckBox', 'switch': 'AXSwitch', 'radio': 'AXRadioButton',
    'menuitemradio': 'AXRadioButton', 'tab': 'AXTab', 'option': 'AXListItem', 'MenuListOption': 'AXListItem',
    'menuitem': 'AXMenuItem', 'treeitem': 'AXTreeItem', 'row': 'AXRow', 'cell': 'AXCell', 'gridcell': 'AXCell',
    'columnheader': 'AXCell', 'rowheader': 'AXCell', 'slider': 'AXSlider', 'spinbutton': 'AXIncrementor',
    'DisclosureTriangle': 'AXDisclosureTriangle', 'StaticText': 'AXStaticText', 'heading': 'AXHeading',
    'image': 'AXImage', 'img': 'AXImage', 'dialog': 'AXGroup', 'alertdialog': 'AXGroup', 'alert': 'AXGroup',
    'status': 'AXGroup', 'group': 'AXGroup', 'region': 'AXGroup', 'form': 'AXGroup', 'navigation': 'AXGroup',
    'search': 'AXGroup', 'listitem': 'AXGroup', 'list': 'AXList', 'listbox': 'AXList', 'menu': 'AXMenu',
    'menubar': 'AXMenuBar', 'table': 'AXTable', 'grid': 'AXTable', 'tree': 'AXOutline', 'tablist': 'AXTabGroup',
    'progressbar': 'AXProgressIndicator'}
STRUCTURAL = frozenset({'RootWebArea', 'WebArea', 'InlineTextBox', 'LineBreak', 'Iframe', 'IframePresentational',
                        'ListMarker', 'none', 'presentation'})
NAMED_ONLY = frozenset({'AXHeading', 'AXImage', 'AXGroup', 'AXList', 'AXMenu', 'AXMenuBar', 'AXTable', 'AXOutline',
                        'AXTabGroup', 'AXRow', 'AXCell', 'AXProgressIndicator'})
# Static text inside these is already their label or value.
CONTAINS_TEXT = frozenset({'AXButton', 'AXLink', 'AXCheckBox', 'AXSwitch', 'AXRadioButton', 'AXTab', 'AXListItem',
                           'AXMenuItem', 'AXTreeItem', 'AXHeading', 'AXCell', 'AXRow', 'AXComboBox',
                           'AXDisclosureTriangle', 'AXTextField', 'AXTextArea', 'AXSearchField',
                           'AXSecureTextField'})
VALUE_ROLES = frozenset({'AXTextField', 'AXTextArea', 'AXSearchField', 'AXComboBox', 'AXSecureTextField',
                         'AXSlider', 'AXIncrementor'})
CHECKED_ROLES = frozenset({'AXCheckBox', 'AXSwitch', 'AXRadioButton'})
PATH = re.compile(r'(m|f[0-9]{1,3})((?:/~[0-9a-f]{8})?(?:/[0-9]{1,6})*)#([0-9]{1,12})(:frame)?')
SECRET_PARAM = re.compile(r'token|code|secret|passw|pwd|key|sig|auth|session|sid|otp|ticket|assertion|saml|nonce|'
                          r'state|jwt|bearer|credential', re.I)
OPAQUE_SCHEMES = ('javascript', 'data', 'blob', 'vbscript', 'file', 'filesystem')
CREDENTIAL_ALLOW = re.compile(r'\b(payment|publickey-credentials-(get|create)|identity-credentials-get|'
                              r'otp-credentials)\b', re.I)
PAYMENT_WORDS = re.compile(r'\b(card|payment|checkout|pay with|billing)\b', re.I)


class CDPError(Exception):
    """A failed CDP call. code: closed, timeout, protocol, or error (the browser answered an error)."""

    CODES = ('closed', 'timeout', 'protocol', 'error')

    def __init__(self, code, detail=''):
        super().__init__(detail or code)
        self.code = code if code in self.CODES else 'error'
        self.detail = ' '.join(str(detail).split())[:300]


class BrowserUnavailable(PolicyError):
    """Pebble's browser cannot start here (E-B1 pending, an unverified or changed browser)."""


# ---- URLs and origins ----------------------------------------------------------

def url_origin(url):
    """The canonical origin of an https URL, or of an http URL on loopback; otherwise None."""
    if not isinstance(url, str) or len(url) > 4 * MAX_URL:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if parts.scheme not in ('https', 'http') or not host or parts.username is not None or parts.password is not None:
        return None
    if parts.scheme == 'http' and host not in LOOPBACK:
        return None
    if port == {'https': 443, 'http': 80}[parts.scheme]:
        port = None
    host = f'[{host}]' if ':' in host else host
    return f'{parts.scheme}://{host}' + (f':{port}' if port else '')


def display_url(url):
    """A URL fit to show and compare: no userinfo, no token-like fragment, credential-like query values
    redacted, and only the scheme of javascript:, data: and similar URLs."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url[:4 * MAX_URL])
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if not scheme:
        return None
    if scheme not in ('http', 'https'):
        return scheme + ':' if scheme in OPAQUE_SCHEMES else url[:MAX_URL]
    host = parts.hostname or ''
    netloc = (f'[{host}]' if ':' in host else host) + (f':{port}' if port and port != {'https': 443, 'http': 80}[scheme]
                                                       else '')
    query = '&'.join(_redact_pair(pair) for pair in parts.query.split('&')) if parts.query else ''
    fragment = parts.fragment if '=' not in parts.fragment and len(parts.fragment) <= 200 else ''
    return urlunsplit((scheme, netloc, parts.path, query, fragment))[:MAX_URL]


def _redact_pair(pair):
    key, sep, value = pair.partition('=')
    if sep and value and SECRET_PARAM.search(unquote(key)):
        return f'{key}=[redacted]'
    return pair


def _frame_origin(frame):
    """A frame's origin when the browser's security origin and URL agree on it; otherwise None (opaque)."""
    origin = url_origin(frame.get('securityOrigin'))
    return origin if origin is not None and origin == url_origin(frame.get('url')) else None


# ---- the pipe -----------------------------------------------------------------------

class CDPConnection:
    """One CDP pipe: JSON messages each followed by a NUL byte, in both directions.

    Owns read_fd (the browser's answers and events) and write_fd (commands).
    Answers are matched by id; events go to listeners on the reader thread.
    Listeners must not call call() (they would wait on their own thread); they
    may notify(). Nothing is ever retried here.
    """

    def __init__(self, read_fd, write_fd, *, timeout=CALL_SECONDS, max_message=MAX_MESSAGE):
        self.read_fd, self.write_fd = read_fd, write_fd
        self.timeout, self.max_message = float(timeout), int(max_message)
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.pending, self.ignored, self.listeners = {}, set(), []
        self.next_id, self.closed, self.reason = 0, False, None
        self._stop = threading.Event()
        self._open = {'read': True, 'write': True}
        self.reader = threading.Thread(target=self._read_loop, name='pebble-cdp-reader', daemon=True)
        self.reader.start()

    def listen(self, listener):
        self.listeners.append(listener)

    def call(self, method, params=None, *, session=None, timeout=None):
        slot = {'event': threading.Event(), 'message': None}
        with self.lock:
            if self.closed:
                raise CDPError('closed', self.reason)
            self.next_id += 1
            mid = self.next_id
            self.pending[mid] = slot
        try:
            self._write(mid, method, params, session)
            if not slot['event'].wait(self.timeout if timeout is None else timeout):
                raise CDPError('timeout', f'{method} did not answer in time.')
        finally:
            with self.lock:
                self.pending.pop(mid, None)
        message = slot['message']
        if message is None:
            raise CDPError('closed', self.reason)
        error = message.get('error')
        if error is not None:
            raise CDPError('error', error.get('message') if isinstance(error, dict) else 'error')
        result = message.get('result')
        return result if isinstance(result, dict) else {}

    def notify(self, method, params=None, *, session=None):
        """Send without waiting (for event listeners). False when the pipe is closed."""
        with self.lock:
            if self.closed:
                return False
            self.next_id += 1
            mid = self.next_id
            if len(self.ignored) > 4096:
                self.ignored.clear()
            self.ignored.add(mid)
        try:
            self._write(mid, method, params, session)
            return True
        except CDPError:
            return False

    def _write(self, mid, method, params, session):
        message = {'id': mid, 'method': method, 'params': params or {}}
        if session is not None:
            message['sessionId'] = session
        data = json.dumps(message, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(data) > self.max_message:
            raise CDPError('protocol', 'The command exceeded its size limit.')
        view = memoryview(data + b'\0')
        with self.write_lock:
            if self.closed:
                raise CDPError('closed', self.reason)
            try:
                while view:
                    view = view[os.write(self.write_fd, view):]
            except OSError as exc:
                self._fail(f'The pipe could not be written ({type(exc).__name__}).')
                raise CDPError('closed', self.reason) from None

    def _fail(self, reason):
        with self.lock:
            if self.closed:
                return
            self.closed, self.reason = True, reason
            slots = list(self.pending.values())
        for slot in slots:
            slot['event'].set()

    def _read_loop(self):
        buffer, reason = bytearray(), 'The browser closed the pipe.'
        poller = select.poll()
        poller.register(self.read_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        try:
            while not self._stop.is_set():
                if not poller.poll(200):
                    continue
                chunk = os.read(self.read_fd, 1 << 20)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    end = buffer.find(b'\0')
                    if end < 0:
                        break
                    raw = bytes(buffer[:end])
                    del buffer[:end + 1]
                    self._deliver(raw)
                if len(buffer) > self.max_message:
                    raise CDPError('protocol', 'A browser message exceeded its size limit.')
        except CDPError as exc:
            reason = exc.detail
        except (OSError, ValueError) as exc:
            reason = f'The pipe failed ({type(exc).__name__}).'
        self._fail(reason)

    def _deliver(self, raw):
        try:
            message = json.loads(raw)
        except ValueError:
            raise CDPError('protocol', 'The browser sent a message that is not JSON.') from None
        if not isinstance(message, dict):
            raise CDPError('protocol', 'The browser sent a message that is not an object.')
        if 'id' in message:
            mid = message['id']
            with self.lock:
                slot = self.pending.get(mid) if type(mid) is int else None
                if slot is None:
                    self.ignored.discard(mid)
                    return
                slot['message'] = message
            slot['event'].set()
            return
        method = message.get('method')
        if isinstance(method, str):
            params = message.get('params') if isinstance(message.get('params'), dict) else {}
            session = message.get('sessionId') if isinstance(message.get('sessionId'), str) else None
            for listener in list(self.listeners):
                try:
                    listener(method, params, session)
                except Exception:  # a listener bug must not stop the pipe; listeners fail closed themselves
                    pass

    def close(self):
        """Close the pipe (the browser exits when its pipe closes) and stop the reader."""
        self._fail('Pebble closed the pipe.')
        self._stop.set()
        with self.write_lock:
            if self._open['write']:
                self._open['write'] = False
                os.close(self.write_fd)
        if threading.current_thread() is not self.reader:
            self.reader.join(timeout=2.0)
        with self.write_lock:
            if self._open['read'] and not self.reader.is_alive():
                self._open['read'] = False
                os.close(self.read_fd)


# ---- launching (refused until E-B1) ---------------------------------------------------

def launch_argv(executable, profile_dir):
    """The browser command line: the CDP pipe, the private profile, and never a TCP debugging port."""
    executable, profile = str(executable), str(profile_dir)
    if not os.path.isabs(executable) or not os.path.isabs(profile) or any(c in profile for c in '\0\n\r'):
        raise ValueError('The browser and its profile need absolute paths.')
    argv = [executable, '--remote-debugging-pipe', f'--user-data-dir={profile}', *LAUNCH_FLAGS, 'about:blank']
    if any(arg.split('=', 1)[0] in FORBIDDEN_FLAGS for arg in argv[1:]):
        raise ValueError('Pebble never opens a TCP debugging port.')
    return argv


def _exit_code(status):
    return -os.WTERMSIG(status) if os.WIFSIGNALED(status) else os.WEXITSTATUS(status)


class PipeProcess:
    """A process this service spawned and has not reaped: the live handle (contract 10).

    Signals go only to an unreaped leader, whose PID and group cannot be reused.
    """

    def __init__(self, pid):
        self.pid, self.returncode = pid, None
        self.lock = threading.Lock()

    def poll(self):
        with self.lock:
            if self.returncode is None:
                try:
                    done, status = os.waitpid(self.pid, os.WNOHANG)
                except ChildProcessError:  # reaped elsewhere: never signal it again
                    done, status = self.pid, None
                if done == self.pid:
                    self.returncode = _exit_code(status) if status is not None else -1
            return self.returncode

    def _signal(self, sig, group=False):
        with self.lock:
            if self.returncode is None:
                try:
                    (os.killpg if group else os.kill)(self.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass

    def terminate(self, grace=3.0, *, clock=time.monotonic, sleep=time.sleep):
        """Wait for the exit the closed pipe causes; then SIGTERM, then SIGKILL the group; then reap."""
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if sig is not None:
                self._signal(sig, group=sig == signal.SIGKILL)
            deadline = clock() + grace
            while self.poll() is None and clock() < deadline:
                sleep(0.05)
            if self.returncode is not None:
                return self.returncode
        with self.lock:
            if self.returncode is None:
                _, status = os.waitpid(self.pid, 0)
                self.returncode = _exit_code(status)
        return self.returncode


def _inheritable_descriptors():
    """This process's open descriptors above stderr that a spawned child would inherit."""
    found = []
    for name in os.listdir('/dev/fd'):
        fd = int(name) if name.isdigit() else -1
        if fd > 2:
            try:
                if os.get_inheritable(fd):
                    found.append(fd)
            except OSError:  # closed meanwhile, or the listing's own descriptor
                pass
    return sorted(found)


def spawn_pipe(argv, env):
    """Spawn argv with fd 3 reading Pebble's commands and fd 4 writing its answers
    (the --remote-debugging-pipe convention), stdio on /dev/null, in a new process group.

    Returns (PipeProcess, CDPConnection)."""
    if not argv or not os.path.isabs(argv[0]):
        raise ValueError('The program needs an absolute path.')
    commands_r, commands_w = os.pipe()
    answers_r, answers_w = os.pipe()
    moved = []
    try:
        for fd in (commands_r, answers_w):  # lift the child's ends above 9 so the dup2 actions cannot collide
            moved.append(fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10))
        # posix_spawn keeps every inheritable descriptor; some come from C libraries (a resolver's socket),
        # not from Pebble's own code. Close them in the child first, so it holds only stdio and fds 3 and 4.
        closes = [(os.POSIX_SPAWN_CLOSE, fd) for fd in _inheritable_descriptors()]
        actions = [*closes,
                   (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                   (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                   (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
                   (os.POSIX_SPAWN_DUP2, moved[0], 3),
                   (os.POSIX_SPAWN_DUP2, moved[1], 4)]
        pid = os.posix_spawn(argv[0], list(argv), dict(env), file_actions=actions, setpgroup=0)
    except BaseException:
        os.close(commands_w)
        os.close(answers_r)
        raise
    finally:
        for fd in (commands_r, answers_w, *moved):
            os.close(fd)
    return PipeProcess(pid), CDPConnection(answers_r, commands_w)


def executable_of(app_path):
    """The main executable of a .app bundle: Contents/MacOS/<CFBundleExecutable>."""
    app = Path(app_path)
    raw = (app / 'Contents/Info.plist').read_bytes()
    if len(raw) > 1_048_576:
        raise PolicyError('The browser bundle cannot be read.')
    info = plistlib.loads(raw)
    name = info.get('CFBundleExecutable') if isinstance(info, dict) else None
    if not isinstance(name, str) or not name or '/' in name or name in ('.', '..'):
        raise PolicyError('The browser bundle names no executable.')
    path = app / 'Contents/MacOS' / name
    if not path.is_file():
        raise PolicyError('The browser bundle names no executable.')
    return str(path)


def _browser_env():
    return {'PATH': '/usr/bin:/bin', 'TMPDIR': os.environ.get('TMPDIR', '/tmp'), 'HOME': str(Path.home())}


class BlockedLauncher:
    """The production launcher until E-B1: refuses before any profile or process exists."""

    available = False
    reason = UNAVAILABLE

    def __init__(self, reason=None):
        if reason:
            self.reason = reason

    def start(self, profile_path):
        raise BrowserUnavailable(self.reason)


class PipeLauncher:
    """Starts the Chromium-family browser the person chose, over a CDP pipe.

    entry is its native_apps.pin() record. start() refuses unless the bundle ID
    is a reviewed Chromium family member with E-B1 evidence in `verified`,
    then re-pins the bundle (bundle, team or signature drift refuses) and spawns it.
    """

    def __init__(self, entry, *, verified=None, families=CHROMIUM_BUNDLES, pin=None, env=None):
        self.entry = dict(entry) if isinstance(entry, dict) else {}
        self.verified = VERIFIED_BROWSERS if verified is None else verified
        self.families, self.pin, self.env = frozenset(families), pin, env

    @property
    def available(self):
        bundle = self.entry.get('bundle_id')
        return bundle in self.families and bundle in self.verified

    @property
    def reason(self):
        if self.entry.get('bundle_id') not in self.families:
            return 'That app is not a supported Chromium-family browser.'
        return None if self.available else UNAVAILABLE

    def start(self, profile_path):
        if not self.available:
            raise BrowserUnavailable(self.reason)
        pin = self.pin
        if pin is None:
            from .native_apps import pin
        current = pin(self.entry.get('id'))
        if any(current.get(k) != self.entry.get(k) for k in ('id', 'bundle_id', 'team_id', 'signature')):
            raise PolicyError('The browser app changed since it was chosen; choose it again in Pebble.')
        record = self.verified.get(self.entry.get('bundle_id'))
        if isinstance(record, dict) and record.get('version') and current.get('version') != record['version']:
            raise BrowserUnavailable(f"The browser was updated since E-B1 checked it ({record['version']} → "
                                     f"{current.get('version')}); E-B1 must run again before Pebble starts it.")
        argv = launch_argv(executable_of(current['id']), profile_path)
        return spawn_pipe(argv, self.env if self.env is not None else _browser_env())


# ---- E-B1 evidence (host-owned; scripts/experiment_eb1.py --record writes it, the parent runs that) ----------

def _private_file(path, limit):
    """The bytes of a regular 0600 file of this user, never through a link; None when absent; PolicyError otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    except OSError:
        raise PolicyError('The E-B1 evidence cannot be read safely.') from None
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
            raise PolicyError('The E-B1 evidence is not a private file of this user.')
        return handle.read(limit + 1)


def eb1_paths(home):
    folder = Path(home) / 'browser' / EB1_DIR
    return folder, folder / EB1_RECORD


def check_eb1_evidence(doc):
    """The bundle facts of an E-B1 evidence document whose every criterion held, or PolicyError."""
    if not isinstance(doc, dict) or doc.get('format') != EB1_FORMAT or doc.get('experiment') != 'E-B1' \
            or doc.get('passed') is not True or not isinstance(doc.get('checks'), dict):
        raise PolicyError('The E-B1 evidence is not a passing E-B1 record.')
    if any(not isinstance(doc['checks'].get(name), dict) or doc['checks'][name].get('passed') is not True
           for name in EB1_CRITERIA):
        raise PolicyError('A criterion in the E-B1 evidence did not hold.')
    bundle = doc['checks']['bundle']
    facts = {k: bundle.get(k) for k in ('bundle_id', 'version', 'team_id', 'signature')}
    if facts['bundle_id'] not in CHROMIUM_BUNDLES or facts['signature'] != 'team' \
            or not isinstance(facts['team_id'], str) or not re.fullmatch(r'[A-Z0-9]{10}', facts['team_id']) \
            or not isinstance(facts['version'], str) or not 1 <= len(facts['version']) <= 64 \
            or not isinstance(doc.get('app'), str) or not doc['app'].endswith('.app') or not os.path.isabs(doc['app']):
        raise PolicyError('The E-B1 evidence names no signed Chromium-family browser.')
    return {**facts, 'app': doc['app']}


def load_verified(home):
    """({bundle_id: record}, None) from the parent's E-B1 record under <home>/browser/e-b1, or ({}, reason).

    The record names an evidence file in the same folder by name and SHA-256. Both
    must be private files of this user; the evidence must be a passing E-B1 run and
    agree with the record on the bundle ID, version, team and app. PipeLauncher then
    also requires the installed browser's version to equal the record's at launch.
    """
    folder, path = eb1_paths(home)
    try:
        raw = _private_file(path, 65_536)
        if raw is None:
            return {}, UNAVAILABLE
        try:
            record = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            raise PolicyError('The E-B1 record is not JSON.') from None
        if not isinstance(record, dict) or record.get('format') != EB1_RECORD_FORMAT \
                or not isinstance(record.get('evidence'), str) or not EB1_FILE.fullmatch(record['evidence']) \
                or not isinstance(record.get('evidence_sha256'), str) \
                or not re.fullmatch(r'[0-9a-f]{64}', record['evidence_sha256']):
            raise PolicyError('The E-B1 record is malformed.')
        evidence = _private_file(folder / record['evidence'], MAX_EVIDENCE)
        if evidence is None or hashlib.sha256(evidence).hexdigest() != record['evidence_sha256']:
            raise PolicyError('The E-B1 evidence is missing or does not match its recorded digest.')
        try:
            facts = check_eb1_evidence(json.loads(evidence.decode('utf-8')))
        except (UnicodeDecodeError, ValueError):
            raise PolicyError('The E-B1 evidence is not JSON.') from None
        if any(record.get(k) != facts[k] for k in ('bundle_id', 'version', 'team_id', 'app')):
            raise PolicyError('The E-B1 record and its evidence disagree.')
    except PolicyError as exc:
        return {}, f'{UNAVAILABLE} ({exc})'
    entry = {'id': facts['app'], 'bundle_id': facts['bundle_id'], 'team_id': facts['team_id'], 'signature': 'team',
             'version': facts['version']}
    return {facts['bundle_id']: {'experiment': 'E-B1', 'version': facts['version'], 'team_id': facts['team_id'],
                                 'evidence_sha256': record['evidence_sha256'], 'stamp': record.get('stamp'),
                                 'entry': entry}}, None


def launcher_from_evidence(home, *, pin=None, env=None):
    """The App's launcher: PipeLauncher for the browser the E-B1 record proves, else BlockedLauncher (with why)."""
    verified, reason = load_verified(home)
    if not verified:
        return BlockedLauncher(reason)
    record = next(iter(verified.values()))
    return PipeLauncher(record['entry'], verified=verified, pin=pin, env=env)


# ---- the private profile ------------------------------------------------------------

class BrowserProfile:
    """Pebble's own browser profile, <home>/browser/profile at mode 0700. Never the person's own profile."""

    def __init__(self, home):
        self.root = Path(home) / 'browser'
        self.path = self.root / 'profile'

    @staticmethod
    def _private(path):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() \
                or info.st_mode & 0o077:
            raise PolicyError("Pebble's browser profile folder is not private, so it was left untouched.")

    def exists(self):
        return os.path.lexists(self.path)

    def ensure(self):
        """Create the profile (0700, with a marker) or check the existing one is Pebble's. Returns its path."""
        created = False
        for path in (self.root, self.path):
            try:
                os.mkdir(path, 0o700)
                created = path == self.path
            except FileExistsError:
                pass
            except FileNotFoundError:
                raise PolicyError("Pebble's data folder is missing.") from None
            self._private(path)
        marker = self.path / PROFILE_MARKER
        if created:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(uuid.uuid4().hex + '\n')
        else:
            try:
                info = os.lstat(marker)
            except FileNotFoundError:
                raise PolicyError("That folder is not Pebble's browser profile, so it was left untouched.") from None
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise PolicyError("That folder is not Pebble's browser profile, so it was left untouched.")
        return self.path

    def forget(self):
        """Delete the profile: cookies, storage, history and cache. Only a private, marked (or empty) folder."""
        if not os.path.lexists(self.path):
            return {'forgotten': False, 'existed': False}
        self._private(self.path)
        with os.scandir(self.path) as entries:
            empty = next(entries, None) is None
        if not empty and not os.path.isfile(self.path / PROFILE_MARKER):
            raise PolicyError("That folder is not Pebble's browser profile, so it was left untouched.")
        shutil.rmtree(self.path)
        return {'forgotten': True, 'existed': True}


# ---- the browser -------------------------------------------------------------------

class BrowserTab:
    """One attached tab. Page events update the counters on the reader thread; adapters hold lock.

    gone is None while attached, 'closed' once the live browser reported the tab
    destroyed, and 'crashed' or 'detached' otherwise. Only 'closed' is ever
    reported as target_gone, which the host may read back as a closed window;
    a crash or a dead pipe is never evidence that an act happened.
    """

    def __init__(self, target_id, session):
        self.target_id, self.session = target_id, session
        self.lock = threading.RLock()
        self.gone, self.main_frame = None, None
        self.requested = self.committed = self.effects = 0
        self.pending = self.dialog = False
        self.guard = None
        self.blocked = []
        self.read_mark = None
        self.frames, self.worlds = {}, {}

    @property
    def alive(self):
        return self.gone is None

    def lose(self, how):
        # 'idle' and 'away' come only from Pebble for Chrome (browser_bridge): detaches it made on purpose.
        if how == 'crashed' or self.gone is None or (how == 'closed' and self.gone in ('detached', 'idle', 'away')):
            self.gone = how

    def mark(self):
        return (self.requested, self.committed)

    def on_event(self, conn, method, params, session):
        if method == 'Fetch.requestPaused':
            self._decide(conn, params, session)
            return
        frame = params.get('frameId')
        main = frame is not None and frame == self.main_frame
        if method == 'Page.frameRequestedNavigation':
            if main and params.get('disposition', 'currentTab') == 'currentTab':
                self.requested += 1
                self.pending = True
        elif method == 'Page.frameStartedLoading':
            if main:
                self.pending = True
        elif method == 'Page.frameStoppedLoading':
            if main:
                self.pending = False
        elif method == 'Page.frameNavigated':
            navigated = params.get('frame') if isinstance(params.get('frame'), dict) else {}
            if not navigated.get('parentId'):
                self.main_frame = navigated.get('id', self.main_frame)
                self.committed += 1
        elif method == 'Page.navigatedWithinDocument':
            if main:
                self.committed += 1
        elif method == 'Page.javascriptDialogOpening':
            self.dialog = True
        elif method == 'Page.javascriptDialogClosed':
            self.dialog = False
        elif method == 'Inspector.targetCrashed':
            self.lose('crashed')
        elif method == 'Inspector.detached':
            self.lose('closed' if params.get('reason') == 'target_closed' else 'detached')

    def _decide(self, conn, params, session):
        """The effect guard: abort main-frame document loads and unsafe methods while armed; else continue.

        Only the method, origin and resource type are kept; request bodies are never read.
        """
        rid = params.get('requestId')
        if not isinstance(rid, str):
            return
        guard = self.guard
        request = params.get('request') if isinstance(params.get('request'), dict) else {}
        method = str(request.get('method') or 'GET').upper()
        kind = params.get('resourceType')
        document = kind == 'Document' and params.get('frameId') == self.main_frame
        if guard is not None and (document or method not in SAFE_METHODS):
            conn.notify('Fetch.failRequest', {'requestId': rid, 'errorReason': 'Aborted'}, session=session)
            self.requested += 1
            if document:
                self.pending = False
            guard['blocked'] += 1
            if len(self.blocked) < MAX_BLOCKED:
                self.blocked.append({'method': method[:16], 'origin': url_origin(request.get('url')) or 'other',
                                     'resource_type': str(kind)[:32]})
        else:
            conn.notify('Fetch.continueRequest', {'requestId': rid}, session=session)


class PebbleBrowser:
    """Pebble's own browser process: one private profile, one CDP pipe, no TCP port.

    start, stop, open_tab and forget_profile are native-only routes. Adapters
    reach tabs through attach(). Locks: attach_lock may be held during CDP
    calls; event_lock never is, because the reader thread takes it.
    """

    def __init__(self, profile, *, launcher=None, timeout=CALL_SECONDS):
        self.profile = profile
        self.launcher = launcher if launcher is not None else BlockedLauncher()
        self.timeout = float(timeout)
        self.lock = threading.RLock()
        self.attach_lock = threading.Lock()
        self.event_lock = threading.Lock()
        self.conn = self.process = self.instance = self.product = None
        self.tabs_by_id, self.tabs_by_session = {}, {}

    @property
    def running(self):
        conn = self.conn
        return conn is not None and not conn.closed

    def reason(self):
        if getattr(self.launcher, 'available', True) is False:
            return getattr(self.launcher, 'reason', None) or UNAVAILABLE
        return "Pebble's browser is not running; start it in Pebble first."

    def status(self):
        return {'running': self.running, 'instance': self.instance if self.running else None,
                'product': self.product if self.running else None, 'profile': str(self.profile.path),
                'profile_exists': self.profile.exists(),
                'available': getattr(self.launcher, 'available', True) is not False,
                'reason': None if self.running else self.reason()}

    def start(self):
        """Native-only: launch the browser over its pipe (refused until E-B1)."""
        with self.lock:
            if self.running:
                return self.status()
            if self.conn is not None:
                self._teardown()
            if getattr(self.launcher, 'available', True) is False:
                raise BrowserUnavailable(self.reason())
            path = self.profile.ensure()
            try:
                process, conn = self.launcher.start(path)
            except OSError:
                raise BrowserUnavailable("Pebble's browser could not be started.") from None
            self.process, self.conn = process, conn
            conn.listen(lambda method, params, session: self._on_event(conn, method, params, session))
            try:
                version = conn.call('Browser.getVersion', timeout=self.timeout)
                conn.call('Browser.setDownloadBehavior', {'behavior': 'deny'}, timeout=self.timeout)
                conn.call('Target.setDiscoverTargets', {'discover': True}, timeout=self.timeout)
            except CDPError:
                self._teardown()
                raise BrowserUnavailable("Pebble's browser did not answer over its pipe.") from None
            self.product = ax_worker.clip(version.get('product'), 64)
            self.instance = uuid.uuid4().hex
            return self.status()

    def stop(self):
        """Native-only: close the pipe and end the browser. True when a browser was running."""
        with self.lock:
            if self.conn is None:
                return False
            self._teardown()
            return True

    def _teardown(self):
        conn, process = self.conn, self.process
        self.conn = self.process = self.instance = None
        with self.event_lock:
            for tab in self.tabs_by_id.values():
                tab.lose('detached')
            self.tabs_by_id.clear()
            self.tabs_by_session.clear()
        if conn is not None:
            if not conn.closed:
                try:
                    conn.call('Browser.close', timeout=2.0)
                except CDPError:
                    pass
            conn.close()
        if process is not None:
            process.terminate()

    def forget_profile(self):
        """Native-only: stop the browser, then delete its private profile."""
        with self.lock:
            stopped = self.stop()
            return {'stopped': stopped, **self.profile.forget()}

    def _connection(self):
        conn = self.conn
        if conn is None or conn.closed:
            raise ScreenRefused('unsupported_target', self.reason())
        return conn

    def tabs(self):
        """Native picker: the tabs of Pebble's browser. Titles are page text; origins come from the browser."""
        try:
            infos = self._connection().call('Target.getTargets', timeout=self.timeout).get('targetInfos')
        except CDPError:
            raise ScreenRefused('ax_failed') from None
        rows = []
        for info in (infos if isinstance(infos, list) else [])[:MAX_TABS]:
            if isinstance(info, dict) and info.get('type') == 'page' and isinstance(info.get('targetId'), str) \
                    and TAB_ID.fullmatch(info['targetId']):
                rows.append({'tab_id': info['targetId'], 'title': ax_worker.clip(info.get('title'), TITLE_SHOWN) or '',
                             'origin': url_origin(info.get('url')), 'url': display_url(info.get('url'))})
        return rows

    def open_tab(self, url):
        """Native-only: open an https URL (or http on loopback) the person typed, in a new tab."""
        if url != 'about:blank' and url_origin(url) is None:
            raise PolicyError('Open an https address, or an http address on this Mac.')
        try:
            target = self._connection().call('Target.createTarget', {'url': url}, timeout=self.timeout).get('targetId')
        except CDPError:
            target = None
        if not isinstance(target, str) or not TAB_ID.fullmatch(target):
            raise PolicyError("Pebble's browser could not open the tab.")
        return {'tab_id': target, 'origin': url_origin(url), 'url': display_url(url)}

    def effect_log(self, tab_id):
        """Host-only: requests the effect guard blocked in a tab (method, origin, resource type)."""
        with self.event_lock:
            tab = self.tabs_by_id.get(tab_id)
            return [dict(item) for item in tab.blocked] if tab else []

    def attach(self, tab_id, instance=None):
        """(tab, instance) for a page target of the running browser; attaches once per tab."""
        with self.attach_lock:
            conn, current = self.conn, self.instance
            if conn is None or conn.closed:
                if instance is not None:
                    raise ScreenRefused('session_expired', RESTARTED)
                raise ScreenRefused('unsupported_target', self.reason())
            if instance is not None and instance != current:
                raise ScreenRefused('session_expired', RESTARTED)
            with self.event_lock:
                tab = self.tabs_by_id.get(tab_id)
            if tab is not None and tab.alive:
                return tab, current
            if tab is not None and instance is not None:  # a session's tab is never silently re-attached
                raise ScreenRefused('target_gone') if tab.gone == 'closed' else ScreenRefused('ax_failed', CRASHED)
            if tab is not None:
                with self.event_lock:
                    self.tabs_by_session.pop(tab.session, None)
            if not isinstance(tab_id, str) or not TAB_ID.fullmatch(tab_id):
                raise ScreenRefused('target_gone')
            session = None
            try:
                infos = conn.call('Target.getTargets', timeout=self.timeout).get('targetInfos')
                if not any(isinstance(i, dict) and i.get('targetId') == tab_id and i.get('type') == 'page'
                           for i in (infos if isinstance(infos, list) else [])):
                    raise ScreenRefused('target_gone')
                session = conn.call('Target.attachToTarget', {'targetId': tab_id, 'flatten': True},
                                    timeout=self.timeout).get('sessionId')
                if not isinstance(session, str):
                    raise ScreenRefused('ax_failed')
                tab = BrowserTab(tab_id, session)
                with self.event_lock:
                    self.tabs_by_session[session] = tab
                conn.call('Page.enable', session=session, timeout=self.timeout)
                conn.call('Page.setInterceptFileChooserDialog', {'enabled': True}, session=session,
                          timeout=self.timeout)
                tree = conn.call('Page.getFrameTree', session=session, timeout=self.timeout).get('frameTree')
                tab.main_frame = ((tree or {}).get('frame') or {}).get('id')
            except (CDPError, ScreenRefused) as exc:
                if session is not None:
                    with self.event_lock:
                        self.tabs_by_session.pop(session, None)
                if isinstance(exc, ScreenRefused):
                    raise
                if exc.code == 'closed' and instance is not None:
                    raise ScreenRefused('session_expired', RESTARTED) from None
                raise ScreenRefused('ax_failed') from None
            with self.event_lock:
                self.tabs_by_id[tab_id] = tab
            return tab, current

    def _on_event(self, conn, method, params, session):
        if conn is not self.conn:
            return
        if session is None:
            if method == 'Target.targetCreated':
                info = params.get('targetInfo') if isinstance(params.get('targetInfo'), dict) else {}
                with self.event_lock:
                    opener = self.tabs_by_id.get(info.get('openerId'))
                if opener is not None:  # a popup the page opened: its requests are not guarded, so it disturbs
                    opener.requested += 1
                return
            how = {'Target.targetDestroyed': 'closed', 'Target.targetCrashed': 'crashed',
                   'Target.detachedFromTarget': 'detached'}.get(method)
            if how is not None:
                with self.event_lock:
                    tab = self.tabs_by_session.get(params.get('sessionId')) if how == 'detached' else \
                        self.tabs_by_id.get(params.get('targetId'))
                if tab is not None:
                    tab.lose(how)
            return
        with self.event_lock:
            tab = self.tabs_by_session.get(session)
        if tab is not None:
            tab.on_event(conn, method, params, session)
        elif method == 'Fetch.requestPaused' and isinstance(params.get('requestId'), str):
            conn.notify('Fetch.continueRequest', {'requestId': params['requestId']}, session=session)


# ---- DOM snapshot and AX facts ------------------------------------------------------

class _Node:
    __slots__ = ('name', 'attrs', 'parent', 'bounds', 'backend')

    def __init__(self, name, attrs, parent, backend):
        self.name, self.attrs, self.parent, self.backend, self.bounds = name, attrs, parent, backend, None


class _Doc:
    """One document of a DOMSnapshot: nodes by backend node ID, its URL, base URL and scroll offset."""

    def __init__(self, frame_id, url, base, scroll):
        self.frame_id, self.url, self.base, self.scroll = frame_id, url, base, scroll
        self.nodes, self.by_index, self.children, self._forms = {}, [], {}, None

    def form_of(self, node):
        ref = node.attrs.get('form')
        if ref:
            if self._forms is None:
                self._forms = {n.attrs['id']: n for n in self.by_index if n.name == 'FORM' and n.attrs.get('id')}
            return self._forms.get(ref)
        index, steps = node.parent, 0
        while 0 <= index < len(self.by_index) and steps < 512:
            parent = self.by_index[index]
            if parent.name == 'FORM':
                return parent
            index, steps = parent.parent, steps + 1
        return None


def _number(value):
    return float(value) if type(value) in (int, float) and abs(value) < 1e7 else 0.0


def _box(value):
    if not isinstance(value, list) or len(value) != 4 or any(type(v) not in (int, float) for v in value):
        return None
    return [float(v) for v in value]


def _documents(raw):
    """{frame_id: _Doc} for a DOMSnapshot.captureSnapshot answer; malformed parts are skipped."""
    strings = raw.get('strings') if isinstance(raw.get('strings'), list) else []

    def text(index):
        return strings[index] if type(index) is int and 0 <= index < len(strings) \
            and isinstance(strings[index], str) else None

    docs, ordered = {}, []
    for item in (raw.get('documents') if isinstance(raw.get('documents'), list) else [])[:MAX_DOCUMENTS]:
        if not isinstance(item, dict):
            ordered.append(None)
            continue
        url = text(item.get('documentURL'))
        doc = _Doc(text(item.get('frameId')), url, text(item.get('baseURL')) or url,
                   (_number(item.get('scrollOffsetX')), _number(item.get('scrollOffsetY'))))
        nodes = item.get('nodes') if isinstance(item.get('nodes'), dict) else {}
        columns = [nodes.get(k) if isinstance(nodes.get(k), list) else [] for k in
                   ('parentIndex', 'nodeName', 'backendNodeId', 'attributes')]
        parents, names, backends, attributes = columns
        for i in range(min(len(parents), len(names), len(backends), MAX_DOM_NODES)):
            row = attributes[i] if i < len(attributes) and isinstance(attributes[i], list) else []
            attrs = {}
            for k in range(0, len(row) - 1, 2):
                key = text(row[k])
                if key:
                    attrs.setdefault(key.lower(), text(row[k + 1]) or '')
            node = _Node((text(names[i]) or '').upper(), attrs, parents[i] if type(parents[i]) is int else -1,
                         backends[i] if type(backends[i]) is int else None)
            doc.by_index.append(node)
            if node.backend is not None:
                doc.nodes.setdefault(node.backend, node)
        layout = item.get('layout') if isinstance(item.get('layout'), dict) else {}
        indexes, bounds = layout.get('nodeIndex') or [], layout.get('bounds') or []
        for position, index in enumerate(indexes[:len(bounds)] if isinstance(indexes, list) else []):
            box = _box(bounds[position])
            if type(index) is not int or not 0 <= index < len(doc.by_index) or box is None:
                continue
            node = doc.by_index[index]
            if node.bounds is None:
                node.bounds = box
            else:  # one node, several boxes (wrapped text): their union
                x, y = min(node.bounds[0], box[0]), min(node.bounds[1], box[1])
                right = max(node.bounds[0] + node.bounds[2], box[0] + box[2])
                bottom = max(node.bounds[1] + node.bounds[3], box[1] + box[3])
                node.bounds = [x, y, right - x, bottom - y]
        rare = nodes.get('contentDocumentIndex') if isinstance(nodes.get('contentDocumentIndex'), dict) else {}
        doc.children = dict(zip(rare.get('index') or [], rare.get('value') or []))
        ordered.append(doc)
    for doc in ordered:
        if doc is None:
            continue
        owners = {}
        for index, child in doc.children.items():
            if type(index) is int and 0 <= index < len(doc.by_index) and type(child) is int \
                    and 0 <= child < len(ordered) and ordered[child] is not None and ordered[child].frame_id:
                owners[doc.by_index[index].backend] = ordered[child].frame_id
        doc.children = owners
        if doc.frame_id and doc.frame_id not in docs:
            docs[doc.frame_id] = doc
    return docs


def _frames(tree):
    """[{'id','parent','url','origin','loader_id','tag'}] for a frame tree, main frame first (pre-order)."""
    out, stack = [], [(tree, None)]
    while stack and len(out) <= MAX_FRAMES:
        item, parent = stack.pop()
        frame = item.get('frame') if isinstance(item, dict) else None
        if not isinstance(frame, dict) or not isinstance(frame.get('id'), str):
            continue
        out.append({'id': frame['id'], 'parent': parent, 'url': frame.get('url'), 'origin': _frame_origin(frame),
                    'loader_id': frame.get('loaderId') if isinstance(frame.get('loaderId'), str) else None,
                    'tag': 'm' if not out else f'f{len(out)}'})
        children = item.get('childFrames') if isinstance(item.get('childFrames'), list) else []
        for child in reversed(children):
            stack.append((child, frame['id']))
    if not out or out[0]['parent'] is not None:
        raise ScreenRefused('ax_failed')
    return out


def _ax_value(field):
    return field.get('value') if isinstance(field, dict) else None


def _ax_text(field):
    value = _ax_value(field)
    return ' '.join(value.split()) if isinstance(value, str) else ''


def _props(node):
    out = {}
    for item in node.get('properties') or ():
        if isinstance(item, dict) and isinstance(item.get('name'), str):
            out.setdefault(item['name'], _ax_value(item.get('value')))
    return out


def _html_type(name, attrs):
    if name == 'INPUT':
        return (attrs.get('type') or 'text').strip().lower()[:32] or 'text'
    if name == 'BUTTON':
        return (attrs.get('type') or 'submit').strip().lower()[:32] or 'submit'
    if name == 'TEXTAREA':
        return 'textarea'
    if name == 'SELECT':
        return 'select-multiple' if 'multiple' in attrs else 'select-one'
    return None


def _facts(node, name, attrs):
    """Role, labels, state and secure flag of one AX node and its DOM element, or None to skip it.

    The snapshot and the pre-dispatch re-check both use this, so a label hash
    taken from either compares equal. The DOM decides a password field; page
    roles and labels can only add secrecy.
    """
    props = _props(node)
    role_name = _ax_value(node.get('role'))
    if name == 'INPUT' and (attrs.get('type') or '').strip().lower() == 'password':
        role = 'AXSecureTextField'
    elif role_name == 'textbox':
        role = 'AXTextArea' if props.get('multiline') is True or name == 'TEXTAREA' else 'AXTextField'
    else:
        role = ROLES.get(role_name)
        if role is None and props.get('focusable') is True and role_name not in STRUCTURAL:
            role = 'AXGroup'
    if role is None:
        return None
    label, description = _ax_text(node.get('name')), _ax_text(node.get('description'))
    if role in NAMED_ONLY and not (label or description) and props.get('focusable') is not True:
        return None
    facts = {'role': role, 'title': None if role == 'AXStaticText' else label or None,
             'description': description or None, 'placeholder': attrs.get('placeholder') or None, 'help': None,
             'enabled': props.get('disabled') is not True, 'focused': props.get('focused') is True,
             'selected': props['selected'] if type(props.get('selected')) is bool else None,
             'expanded': props['expanded'] if type(props.get('expanded')) is bool else None,
             'editable': True if props.get('editable') in ('plaintext', 'richtext') else None,
             'multiline': True if props.get('multiline') is True or name == 'TEXTAREA' else None,
             'html_type': _html_type(name, attrs), 'autocomplete': attrs.get('autocomplete') or None, 'value': None}
    if role == 'AXStaticText':
        facts['value'] = label
    elif role in CHECKED_ROLES:
        facts['value'] = {'true': True, 'false': False}.get(str(props.get('checked')).lower())
    elif role in VALUE_ROLES or facts['editable']:
        value = _ax_value(node.get('value'))
        facts['value'] = value if isinstance(value, (str, int, float)) and type(value) is not bool else ''
    facts['secure'] = ax_worker.is_secure({**facts, 'secure': None})
    if facts['secure']:
        facts['value'] = None  # never leaves the adapter
    return facts


def _path(tag, chain, backend, embedded=False):
    """Positional and unique: frame tag, AX child indexes, then the backend node ID that pins the node."""
    steps = ''.join(f'/{i}' for i in chain)
    if len(steps) > PATH_STEPS:
        digest = hashlib.sha256(steps.encode('ascii')).hexdigest()[:8]
        steps = f'/~{digest}' + ''.join(f'/{i}' for i in chain[-CHAIN_KEEP:])
    return f'{tag}{steps}#{backend}' + (':frame' if embedded else '')


def _viewport(node, offset):
    if node is None or node.bounds is None:
        return None
    x, y, w, h = node.bounds
    return [x + offset[0], y + offset[1], w, h]


def _credential_frame(attrs, hint):
    return bool(CREDENTIAL_ALLOW.search(attrs.get('allow') or '') or LOGIN_ACTION.search(hint)
                or ax_worker.SECRET_LABEL.search(hint) or TWO_FACTOR.search(hint) or PAYMENT_WORDS.search(hint))


class _Walk:
    """Builds the element list of granted frames from their AX trees, never entering another document.

    skip names DOM elements whose whole subtree is left out (Pebble for Chrome's own
    in-page indicator); a page using that name only hides its own content from Pebble.
    """

    def __init__(self, limit, origins, frames, docs, owners, fetch, skip=frozenset()):
        self.limit, self.origins = limit, set(origins)
        self.frames = {f['id']: f for f in frames}
        self.docs, self.owners, self.fetch = docs, owners, fetch
        self.skip = frozenset(skip)
        self.elements, self.truncated, self.visited = [], False, set()

    def _add(self, element):
        if len(self.elements) >= self.limit:
            self.truncated = True
            return False
        self.elements.append(element)
        return True

    def frame(self, frame, offset):
        if frame['id'] in self.visited or self.truncated:
            return
        self.visited.add(frame['id'])
        doc = self.docs[frame['id']]
        nodes = [n for n in self.fetch(frame['id'])[:MAX_AX_NODES] if isinstance(n, dict)]
        if len(nodes) >= MAX_AX_NODES:
            self.truncated = True
        index = {}
        for node in nodes:
            if isinstance(node.get('nodeId'), str):
                index.setdefault(node['nodeId'], node)
        roots = [n for n in nodes if not n.get('parentId')][:1] or nodes[:1]
        stack, seen = [(roots[0], (), False)] if roots else [], set()
        while stack and not self.truncated:
            node, chain, inside = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            backend = node.get('backendDOMNodeId')
            if self.skip and type(backend) is int and backend in doc.nodes and doc.nodes[backend].name in self.skip:
                continue
            if backend in self.owners or _ax_value(node.get('role')) in ('Iframe', 'IframePresentational'):
                self._embedded(node, doc, frame, chain, offset)
                continue  # another document is never entered from here
            contains = False
            if not node.get('ignored') and type(backend) is int and backend in doc.nodes:
                element = self._element(node, doc.nodes[backend], doc, frame['tag'], chain, offset, inside)
                if element is not None:
                    if not self._add(element):
                        return
                    contains = element['role'] in CONTAINS_TEXT
            children = [index[c] for c in node.get('childIds') or () if isinstance(c, str) and c in index]
            for position in reversed(range(len(children))):
                stack.append((children[position], chain + (position,), inside or contains))

    def _element(self, node, dom, doc, tag, chain, offset, inside):
        facts = _facts(node, dom.name, dom.attrs)
        if facts is None or facts['role'] == 'AXStaticText' and (inside or not facts['value']):
            return None
        rect = _viewport(dom, offset)
        if rect is None or rect[2] <= 0 or rect[3] <= 0:
            return None  # not rendered
        facts.update(path=_path(tag, chain, dom.backend), frame=rect)
        if dom.name in ('A', 'AREA') and 'href' in dom.attrs:
            facts['href'] = display_url(urljoin(doc.base or '', dom.attrs['href'].strip()))
            facts['download'] = True if 'download' in dom.attrs else None
        elif facts['html_type'] in ('submit', 'image') and dom.name in ('BUTTON', 'INPUT'):
            form = doc.form_of(dom)
            if form is not None:  # the review shows where the form sends
                action = (dom.attrs.get('formaction') or form.attrs.get('action') or '').strip()
                facts['href'] = display_url(urljoin(doc.base or '', action) if action else doc.url)
        return facts

    def _embedded(self, node, doc, frame, chain, offset):
        backend = node.get('backendDOMNodeId')
        dom = doc.nodes.get(backend) if type(backend) is int else None
        rect = _viewport(dom, offset)
        child = self.frames.get(self.owners.get(backend))
        if child is not None and child['origin'] in self.origins and child['id'] in self.docs and rect is not None:
            inner = self.docs[child['id']]
            self.frame(child, (rect[0] - inner.scroll[0], rect[1] - inner.scroll[1]))
            return
        if dom is None or rect is None or rect[2] * rect[3] < MIN_FRAME_AREA:
            return
        attrs = dom.attrs
        hint = ' '.join(t for t in (attrs.get('title'), attrs.get('name'), attrs.get('aria-label'),
                                    _ax_text(node.get('name'))) if t)[:ax_worker.MAX_LABEL_SOURCE]
        where = child['origin'] if child is not None and child['origin'] else 'another site'
        self._add({'role': 'AXGroup', 'path': _path(frame['tag'], chain, dom.backend, embedded=True),
                   'description': f'Embedded content from {where} (not observed; Pebble cannot act inside it)',
                   'help': hint or None, 'frame': rect, 'enabled': True,
                   'secure': _credential_frame(attrs, hint)})


def _quad_rect(quad):
    if not isinstance(quad, list) or len(quad) != 8 or any(type(v) not in (int, float) for v in quad):
        return None
    xs, ys = quad[0::2], quad[1::2]
    return [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]


def _near(a, b, tolerance):
    return max(abs(x - y) for x, y in zip(a, b)) <= tolerance


def _attr_pairs(flat):
    out = {}
    if isinstance(flat, list):
        for k in range(0, len(flat) - 1, 2):
            if isinstance(flat[k], str) and isinstance(flat[k + 1], str):
                out.setdefault(flat[k].lower(), flat[k + 1])
    return out


# ---- the screen adapter ----------------------------------------------------------------

class BrowserAdapter:
    """screen_session adapter for {kind:'browser', profile:'pebble', tab_id, origins} targets."""

    kind = 'browser'
    SKIP_TAGS = frozenset()  # DOM names never read (browser_bridge.ChromeAdapter skips its page indicator)

    def __init__(self, browser, *, frame_tolerance=FRAME_TOLERANCE, settle_seconds=SETTLE_SECONDS,
                 navigation_wait=NAVIGATION_WAIT, clock=time.monotonic, sleep=time.sleep):
        self.browser = browser
        self.frame_tolerance, self.settle_seconds = int(frame_tolerance), float(settle_seconds)
        self.navigation_wait, self.clock, self.sleep = float(navigation_wait), clock, sleep

    # -- the contract --

    def windows(self, target):
        return self.browser.tabs()

    def pin(self, target):
        tab, instance = self.browser.attach(target['tab_id'])
        return {'instance': instance, 'tab_id': tab.target_id}

    def focus_window(self, target, pin):
        tab = self._tab(target, pin)
        with tab.lock:
            self._call('Target.activateTarget', {'targetId': tab.target_id})
            self._call('Page.bringToFront', session=tab.session)

    def snapshot(self, target, pin, limit):
        tab = self._tab(target, pin)
        with tab.lock:
            try:
                for _ in range(READ_ATTEMPTS):
                    self._settle(tab)
                    if tab.dialog:
                        raise ScreenRefused('stale_ui', DIALOG)
                    mark = tab.mark()
                    snap = self._read(tab, target, limit)
                    self._sync(tab)  # every event sent before the last answer has been applied
                    if tab.mark() == mark and not tab.dialog and tab.alive:
                        tab.read_mark = mark
                        return snap
            except ScreenRefused:
                if not tab.alive or not self.browser.running:
                    raise self._lost(tab) from None
                raise
            if not tab.alive:
                raise self._lost(tab)
        raise ScreenRefused('stale_ui', 'The page kept navigating while Pebble read it.')

    def perform(self, target, pin, element, action, text):
        """Dispatch one act once. A refusal carries facts['dispatched']: whether any effect call was sent."""
        tab = self._tab(target, pin)
        with tab.lock:
            sent = tab.effects
            try:
                self._perform(tab, target, element, action, text)
            except ScreenRefused as exc:
                exc.facts.setdefault('dispatched', tab.effects != sent)
                raise

    def _perform(self, tab, target, element, action, text):
        match = PATH.fullmatch(element.get('path') or '')
        if not match:
            raise ScreenRefused('stale_ui', 'That element does not belong to this browser tab.')
        if match.group(4):
            raise ScreenRefused('unsupported_target', EMBEDDED)
        frame, backend = tab.frames.get(match.group(1)), int(match.group(3))
        if frame is None:
            raise ScreenRefused('stale_ui')
        self._current(tab)
        klass, _ = classify(action, element, kind='browser', origins=target['origins'], text=text)
        name, rect = self._recheck(tab, element, backend)
        guard = klass != 'commit'
        if guard:
            self._arm(tab)
        try:
            self._dispatch(tab, frame, element, backend, name, rect, action, text)
        finally:
            if guard:
                self._disarm(tab)
            conn = self.browser.conn
            if conn is not None:
                conn.notify('Runtime.releaseObjectGroup', {'objectGroup': OBJECT_GROUP}, session=tab.session)

    # -- reading --

    def _tab(self, target, pin):
        if not isinstance(pin, dict) or pin.get('tab_id') != target.get('tab_id') or not pin.get('instance'):
            raise ScreenRefused('session_expired', RESTARTED)
        tab, _ = self.browser.attach(target['tab_id'], pin['instance'])
        if not tab.alive:
            raise self._lost(tab)
        return tab

    def _lost(self, tab):
        """target_gone only when the live browser reported the tab closed; never for a crash or a dead pipe."""
        if not self.browser.running:
            return ScreenRefused('session_expired', RESTARTED)
        if tab.gone == 'closed':
            return ScreenRefused('target_gone')
        return ScreenRefused('ax_failed', CRASHED)

    def _call(self, method, params=None, *, session=None, effect=False):
        conn = self.browser.conn
        if conn is None:
            if effect:
                raise AXError('uncertain', f'{method}: the browser is gone')
            raise ScreenRefused('session_expired', RESTARTED)
        try:
            return conn.call(method, params, session=session, timeout=self.browser.timeout)
        except CDPError as exc:
            if effect:  # the act may or may not have happened; never retried
                raise AXError('uncertain', f'{method}: {exc.code}') from None
            if exc.code == 'closed':
                raise ScreenRefused('session_expired', RESTARTED) from None
            raise ScreenRefused('ax_timeout' if exc.code == 'timeout' else 'ax_failed') from None

    def _sync(self, tab):
        """A cheap round trip: when it answers, every event the browser sent before has been handled."""
        return self._call('Page.getNavigationHistory', session=tab.session)

    def _settle(self, tab):
        self._sync(tab)
        deadline = self.clock() + self.navigation_wait
        while tab.pending and tab.alive and self.clock() < deadline:
            self.sleep(NAVIGATION_POLL)
            self._sync(tab)

    def _world(self, tab, frame):
        key = (frame['id'], frame['loader_id'])
        world = tab.worlds.get(key)
        if world is None:
            # Phase 24 (V1-153): the CDP parameter is grantUniversalAccess; the misspelt key never reached Chrome, which
            # only defaulted to false. lib/policy.mjs checks the same key (pinned by test_browser_bridge_22.py).
            world = self._call('Page.createIsolatedWorld', {'frameId': frame['id'], 'worldName': WORLD,
                                                            'grantUniversalAccess': False},
                               session=tab.session).get('executionContextId')
            if type(world) is not int:
                raise ScreenRefused('ax_failed')
            if len(tab.worlds) >= 32:
                tab.worlds.clear()
            tab.worlds[key] = world
        return world

    def _read(self, tab, target, limit):
        session = tab.session
        frames = _frames(self._call('Page.getFrameTree', session=session).get('frameTree'))
        main = frames[0]
        tab.main_frame = main['id']
        history = self._call('Page.getNavigationHistory', session=session)
        entries, current = history.get('entries'), history.get('currentIndex')
        if not isinstance(entries, list) or type(current) is not int or not 0 <= current < len(entries) \
                or not isinstance(entries[current], dict):
            raise ScreenRefused('ax_failed')
        info = self._call('Target.getTargetInfo', {'targetId': tab.target_id}).get('targetInfo') or {}
        tab.frames = {f['tag']: f for f in frames}
        window = {'tab_id': tab.target_id, 'title': info.get('title') if isinstance(info.get('title'), str) else '',
                  'url': display_url(main['url']), 'origin': main['origin'],
                  'navigation_id': f"{entries[current].get('id')}.{tab.requested}", 'loader_id': main['loader_id'],
                  'focused': False, 'frame': None}
        if main['origin'] is None or main['origin'] not in target['origins']:
            return {'window': window, 'elements': [], 'truncated': False}  # never read outside the grant
        try:
            state = self._call('Runtime.evaluate', {'expression': VISIBILITY_JS, 'returnByValue': True,
                                                    'contextId': self._world(tab, main)}, session=session)
        except ScreenRefused:
            tab.worlds.clear()
            raise
        window['focused'] = (state.get('result') or {}).get('value') == 'visible'
        docs = _documents(self._call('DOMSnapshot.captureSnapshot', {'computedStyles': [], 'includeDOMRects': True},
                                     session=session))
        if main['id'] not in docs:
            raise ScreenRefused('ax_failed')
        owners = {}
        for doc in docs.values():
            owners.update(doc.children)
        for frame in frames[1:]:
            if frame['id'] in owners.values():
                continue
            try:  # an out-of-process frame: only its owner element is looked up, never its content
                backend = self._call('DOM.getFrameOwner', {'frameId': frame['id']}, session=session).get(
                    'backendNodeId')
            except ScreenRefused:
                continue
            if type(backend) is int:
                owners[backend] = frame['id']
        walk = _Walk(limit, target['origins'], frames, docs, owners,
                     lambda frame_id: self._ax(tab, frame_id), skip=self.SKIP_TAGS)
        scroll = docs[main['id']].scroll
        walk.frame(main, (-scroll[0], -scroll[1]))
        return {'window': window, 'elements': walk.elements, 'truncated': walk.truncated}

    def _ax(self, tab, frame_id):
        nodes = self._call('Accessibility.getFullAXTree', {'frameId': frame_id}, session=tab.session).get('nodes')
        return nodes if isinstance(nodes, list) else []

    # -- acting --

    def _current(self, tab):
        """Refuse when the tab navigated, opened a dialog or vanished since the host's fresh snapshot."""
        self._sync(tab)
        if not tab.alive:
            raise self._lost(tab)
        if tab.dialog:
            raise ScreenRefused('stale_ui', DIALOG)
        if tab.read_mark is None or tab.mark() != tab.read_mark:
            raise ScreenRefused('stale_ui', 'The page navigated after Pebble checked it.')

    def _recheck(self, tab, element, backend):
        """(node name, viewport box) after re-checking the live node against the host's element."""
        conn, session = self.browser.conn, tab.session
        if conn is None:
            raise ScreenRefused('session_expired', RESTARTED)
        try:
            quads = conn.call('DOM.getContentQuads', {'backendNodeId': backend}, session=session,
                              timeout=self.browser.timeout).get('quads')
            described = conn.call('DOM.describeNode', {'backendNodeId': backend}, session=session,
                                  timeout=self.browser.timeout).get('node')
            partial = conn.call('Accessibility.getPartialAXTree', {'backendNodeId': backend, 'fetchRelatives': False},
                                session=session, timeout=self.browser.timeout).get('nodes')
        except CDPError as exc:
            if exc.code == 'error':
                raise ScreenRefused('stale_ui', 'The element is gone from the page.') from None
            if exc.code == 'closed':
                raise ScreenRefused('session_expired', RESTARTED) from None
            raise ScreenRefused('ax_timeout' if exc.code == 'timeout' else 'ax_failed') from None
        rect = _quad_rect(quads[0]) if isinstance(quads, list) and quads else None
        if rect is None:
            raise ScreenRefused('stale_ui', 'The element is no longer rendered.')
        if element.get('frame') is not None and not _near(rect, element['frame'], self.frame_tolerance):
            raise ScreenRefused('stale_ui', f'The element moved more than {self.frame_tolerance} px.')
        node = next((n for n in partial if isinstance(n, dict) and n.get('backendDOMNodeId') == backend), None) \
            if isinstance(partial, list) else None
        described = described if isinstance(described, dict) else {}
        name = str(described.get('nodeName') or '').upper()
        facts = _facts(node, name, _attr_pairs(described.get('attributes'))) if node else None
        clean = sanitize_element({**facts, 'path': element.get('path')}) if facts else None
        if clean is None or clean['role'] != element.get('role') or label_sha256(clean) != element.get('label_sha256') \
                or clean['secure'] != element.get('secure'):
            raise ScreenRefused('stale_ui', 'The element changed its role, label or kind.')
        return name, rect

    def _arm(self, tab):
        tab.guard = {'blocked': 0}
        try:
            self._call('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]},
                       session=tab.session)
        except ScreenRefused:
            tab.guard = None
            raise

    def _disarm(self, tab):
        try:
            self.sleep(self.settle_seconds)
            self._sync(tab)  # every request paused before this answer has been decided
            self._call('Fetch.disable', session=tab.session)
        except ScreenRefused:
            pass
        finally:
            tab.guard = None

    def _effect(self, tab, method, params):
        tab.effects += 1  # counted before sending: from here on the act may have happened
        return self._call(method, params, session=tab.session, effect=True)

    def _object(self, tab, frame, backend):
        result = self._call('DOM.resolveNode', {'backendNodeId': backend, 'objectGroup': OBJECT_GROUP,
                                                'executionContextId': self._world(tab, frame)}, session=tab.session)
        object_id = (result.get('object') or {}).get('objectId')
        if not isinstance(object_id, str):
            raise ScreenRefused('stale_ui', 'The element is gone from the page.')
        return object_id

    def _apply(self, tab, object_id, js, args):
        result = self._effect(tab, 'Runtime.callFunctionOn', {'functionDeclaration': js, 'objectId': object_id,
                                                              'arguments': [{'value': a} for a in args],
                                                              'returnByValue': True})
        return (result.get('result') or {}).get('value')

    def _function(self, tab, frame, backend, js, args):
        object_id = self._object(tab, frame, backend)
        self._current(tab)
        return self._apply(tab, object_id, js, args)

    def _focused(self, tab, object_id):
        """Keys go to whatever has focus, so the page must not have moved it (to a secret field or a frame)."""
        result = self._call('Runtime.callFunctionOn', {'functionDeclaration': FOCUSED_JS, 'objectId': object_id,
                                                       'returnByValue': True}, session=tab.session)
        if (result.get('result') or {}).get('value') is not True:
            raise ScreenRefused('stale_ui', 'The page moved the keyboard focus away from the element, so Pebble '
                                            'stopped before typing there.')

    def _contains(self, tab, frame, backend, other):
        if type(other) is not int:
            return False
        try:
            outer, inner = self._object(tab, frame, backend), self._object(tab, frame, other)
            result = self._call('Runtime.callFunctionOn', {'functionDeclaration': CONTAINS_JS, 'objectId': outer,
                                                           'arguments': [{'objectId': inner}], 'returnByValue': True},
                                session=tab.session)
        except ScreenRefused:
            return False
        return (result.get('result') or {}).get('value') is True

    def _click(self, tab, frame, backend, rect):
        x, y = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
        hit = self._call('DOM.getNodeForLocation', {'x': int(round(x)), 'y': int(round(y)),
                                                    'includeUserAgentShadowDOM': True,
                                                    'ignorePointerEventsNone': False}, session=tab.session)
        other = hit.get('backendNodeId')
        if other != backend and (hit.get('frameId') not in (None, frame['id'])
                                 or not self._contains(tab, frame, backend, other)):
            raise ScreenRefused('stale_ui', 'Something else covers the element now.')
        self._current(tab)
        self._effect(tab, 'Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
        for kind, buttons in (('mousePressed', 1), ('mouseReleased', 0)):
            self._effect(tab, 'Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left',
                                                           'buttons': buttons, 'clickCount': 1})

    def _key(self, tab, key):
        self._effect(tab, 'Input.dispatchKeyEvent', {'type': 'keyDown', **key})
        self._effect(tab, 'Input.dispatchKeyEvent', {'type': 'keyUp', **{k: v for k, v in key.items() if k != 'text'}})

    def _dispatch(self, tab, frame, element, backend, name, rect, action, text):
        if action in ('focus', 'scroll'):
            self._current(tab)
            self._effect(tab, 'DOM.focus' if action == 'focus' else 'DOM.scrollIntoViewIfNeeded',
                         {'backendNodeId': backend})
        elif action == 'enter':
            target = self._object(tab, frame, backend)
            self._current(tab)
            self._effect(tab, 'DOM.focus', {'backendNodeId': backend})
            self._focused(tab, target)
            self._key(tab, ENTER)
        elif action in ('expand', 'collapse', 'toggle') and name in ('SUMMARY', 'DETAILS'):
            want = element.get('expanded') is not True if action == 'toggle' else action == 'expand'
            if element.get('expanded') is not want:
                self._function(tab, frame, backend, DETAILS_JS, [want])
        elif action in ('expand', 'collapse') and element.get('expanded') is (action == 'expand'):
            return  # already in that state; nothing to dispatch
        elif action == 'select' and name == 'OPTION':
            self._function(tab, frame, backend, OPTION_JS, [])
        elif action == 'type' and name == 'SELECT':
            self._function(tab, frame, backend, CHOOSE_JS, [text])
        elif action == 'type':
            target = self._object(tab, frame, backend)
            self._current(tab)
            self._apply(tab, target, SELECT_ALL_JS, [])  # focus and select the whole value
            lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
            steps = [BACKSPACE] if text == '' else [item for index, line in enumerate(lines)
                                                     for item in ([ENTER] if index else []) + ([line] if line else [])]
            for step in steps:
                self._focused(tab, target)
                if isinstance(step, dict):
                    self._key(tab, step)
                else:
                    self._effect(tab, 'Input.insertText', {'text': step})
        else:  # press, toggle, select, expand, collapse: a trusted click on the re-checked element
            self._click(tab, frame, backend, rect)


# ---- parent helpers -------------------------------------------------------------------------

def revoke_browser_sessions(sessions, profile='pebble'):
    """Revoke every live screen session in Pebble's own browser (before the profile goes). Returns how many ended.

    Sessions in the person's Chrome (profile 'chrome', Phase 22) are left alone: stopping or forgetting
    Pebble's browser does not touch the person's tabs. profile=None revokes every browser session.
    """
    rows = sessions.store.query("SELECT session_id,target FROM screen_sessions WHERE kind='browser' AND state IN "
                                "('active','handoff','interrupted')")
    count = 0
    for row in rows:
        try:
            target = json.loads(row['target'])
        except (TypeError, ValueError):
            target = {}
        if profile is None or (target.get('profile', 'pebble') if isinstance(target, dict) else 'pebble') == profile:
            count += sessions.revoke(row['session_id'])
    return count


def forget_browser_profile(browser, sessions=None):
    """The `browser_profile_forget` native route: revoke browser sessions, stop the browser, delete the profile."""
    revoked = revoke_browser_sessions(sessions) if sessions is not None else 0
    return {'revoked_sessions': revoked, **browser.forget_profile()}
