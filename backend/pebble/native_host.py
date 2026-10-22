"""Pebble for Chrome's native messaging host, `com.pebble.browser` (Phase 22).

Chrome starts this process when the extension calls chrome.runtime.connectNative
and passes the calling extension's origin as an argument. The host relays
messages between the extension (stdio: a 32-bit native-order length, then UTF-8
JSON) and the running Pebble backend (the Unix socket <home>/chrome/bridge.sock:
the same framing, little-endian). It is a plain relay with three jobs:

1. Refuse any origin but the pinned extension. Chrome's allowed_origins already
   enforces this; the check here is a second fence, not authentication.
2. Authenticate to the backend. It reads the token in <home>/chrome/token (a
   0600 file of this user, rewritten at every Pebble start), checks that the
   folder and the socket are this user's and that the process at the other end
   of the socket is this user's, then sends the hello. The extension never sees
   the token. Nothing else is read: no vault, Keychain, database or credential.
   After the hello, the extension and the backend seal every message with their
   paired key (browser_bridge); the host relays sealed frames it cannot open.
3. Cap every message at MAX_FRAME (1 MiB) in both directions and require a JSON
   object with a string `type`. Anything else ends the relay.

It exits when either side closes. It imports only the standard library, so it
carries no Pebble state. The installed app runs it as
`pebble-backend --browser-native-host --home <data home> <origin>` through the
launcher that scripts/install_native_host.py writes; browser_bridge holds the
server side and pins these constants to its own.
"""

import json
import os
import socket
import stat
import struct
import sys
import threading

PROTOCOL = 'pebble-browser/1'
EXTENSION_ID = 'ilmkbniehiecgoodnbkkcjgaelebehbj'
EXTENSION_ORIGIN = f'chrome-extension://{EXTENSION_ID}/'
BRIDGE_DIR, SOCKET_NAME, TOKEN_NAME = 'chrome', 'bridge.sock', 'token'
MAX_FRAME = 1 << 20
HOST_VERSION = '1'
CONNECT_SECONDS = 2.0
EXIT_OK, EXIT_USAGE, EXIT_ORIGIN, EXIT_NO_APP, EXIT_PROTOCOL = 0, 2, 3, 4, 5
NO_APP = 'Pebble is not running. Open Pebble, then try again.'
REFUSED = 'Pebble did not accept this connection. Restart Pebble, then try again.'


class Ended(Exception):
    """The relay must stop. code is short and content-free."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _no_constants(name):
    raise ValueError(name)


def _exact(stream, count):
    buffer = bytearray()
    while len(buffer) < count:
        chunk = stream.read(count - len(buffer)) if hasattr(stream, 'read') else stream.recv(count - len(buffer))
        if not chunk:
            if not buffer:
                return None
            raise Ended('truncated')
        buffer += chunk
    return bytes(buffer)


def read_message(stream, byteorder=sys.byteorder):
    """One JSON object from a file-like (read) or a socket (recv), or None at a clean end of stream."""
    header = _exact(stream, 4)
    if header is None:
        return None
    size = int.from_bytes(header, byteorder)
    if size > MAX_FRAME:
        raise Ended('too_large')
    body = _exact(stream, size) if size else b''
    if body is None:
        raise Ended('truncated')
    try:
        message = json.loads(body.decode('utf-8'), parse_constant=_no_constants)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Ended('not_json') from None
    if not isinstance(message, dict) or not isinstance(message.get('type'), str):
        raise Ended('not_message')
    return message


def encode(message, byteorder=sys.byteorder):
    data = json.dumps(message, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(data) > MAX_FRAME:
        raise Ended('too_large')
    return len(data).to_bytes(4, byteorder) + data


def write_message(stream, message, byteorder=sys.byteorder):
    stream.write(encode(message, byteorder))
    stream.flush()


def peer_uid(sock):
    """The effective user ID at the other end of a Unix socket (as browser_bridge.peer_uid)."""
    if sys.platform == 'darwin':
        raw = sock.getsockopt(getattr(socket, 'SOL_LOCAL', 0), getattr(socket, 'LOCAL_PEERCRED', 0x001), 76)
        if len(raw) < 8:
            raise OSError('short peer credentials')
        version, uid = struct.unpack_from('=II', raw)
        if version != 0:
            raise OSError('unknown peer credential layout')
        return uid
    option = getattr(socket, 'SO_PEERCRED', None)
    if option is None:
        raise OSError('peer credentials are not available here')
    _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, option, struct.calcsize('3i')))
    return uid


def _private(path, kind):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise OSError('not private')
    if kind == 'dir' and (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077):
        raise OSError('not private')
    if kind == 'socket' and not stat.S_ISSOCK(info.st_mode):
        raise OSError('not a socket')
    return info


def read_token(folder):
    fd = os.open(os.path.join(folder, TOKEN_NAME), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 \
                or info.st_size != 64:
            raise OSError('token not private')
        text = handle.read(65).decode('ascii', 'replace')
    if len(text) != 64 or any(c not in '0123456789abcdef' for c in text):
        raise OSError('token malformed')
    return text


def connect(home, *, peer=peer_uid, timeout=CONNECT_SECONDS):
    """(socket, token) for this user's running Pebble, or OSError."""
    folder = os.path.join(home, BRIDGE_DIR)
    _private(folder, 'dir')
    path = os.path.join(folder, SOCKET_NAME)
    _private(path, 'socket')
    token = read_token(folder)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(path)
        if peer(sock) != os.getuid():
            raise OSError('the socket belongs to another user')
    except BaseException:
        sock.close()
        raise
    return sock, token


def parse_args(argv):
    """(home, origin) from `--home PATH <origin> [--parent-window=N]`; Chrome appends the origin."""
    home, origin, rest = None, None, list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == '--home' and rest:
            home = rest.pop(0)
        elif arg.startswith('--home='):
            home = arg.split('=', 1)[1]
        elif arg.startswith('--parent-window='):
            continue  # Windows only; ignored
        elif not arg.startswith('-') and origin is None:
            origin = arg
        else:
            return None, None
    if not home or not os.path.isabs(home) or any(c in home for c in '\0\n\r'):
        return None, origin
    return home, origin


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else None


def _hex(value, size):
    return isinstance(value, str) and len(value) == size and all(c in '0123456789abcdef' for c in value)


def _pump(source, sink, read_order, write_order, check):
    """Copy messages until source ends; raises Ended on a bad message."""
    while True:
        message = read_message(source, read_order)
        if message is None:
            return
        check(message)
        data = encode(message, write_order)
        if hasattr(sink, 'sendall'):
            sink.sendall(data)
        else:
            sink.write(data)
            sink.flush()


def _from_extension(message):
    if message['type'] == 'hello':
        raise Ended('second_hello')


def main(argv=None, stdin=None, stdout=None, *, peer=peer_uid):
    argv = sys.argv[1:] if argv is None else argv
    # Chrome's stdin, unbuffered: the upstream thread may still be blocked reading it when the relay ends (Pebble quit
    # or broke the framing), and CPython aborts at shutdown if that thread holds a BufferedReader's lock.
    stdin = stdin if stdin is not None else sys.stdin.buffer.raw
    stdout = stdout if stdout is not None else sys.stdout.buffer
    home, origin = parse_args(argv)
    if origin != EXTENSION_ORIGIN:
        return EXIT_ORIGIN  # not Pebble's extension: say nothing to it
    if home is None:
        return EXIT_USAGE
    try:
        hello = read_message(stdin)
    except Ended:
        return EXIT_PROTOCOL
    if hello is None:
        return EXIT_OK
    if hello.get('type') != 'hello' or hello.get('protocol') != PROTOCOL:
        return EXIT_PROTOCOL
    try:
        sock, token = connect(home, peer=peer)
    except (FileNotFoundError, ConnectionRefusedError):
        write_message(stdout, {'type': 'status', 'state': 'app_not_running', 'message': NO_APP})
        return EXIT_NO_APP
    except OSError:  # a folder, token or socket that is not this user's and private, or another user at the other end
        write_message(stdout, {'type': 'status', 'state': 'refused', 'message': REFUSED})
        return EXIT_PROTOCOL
    extension = hello.get('extension') if isinstance(hello.get('extension'), dict) else {}
    # The extension's pairing ID and nonce pass through untouched (hex only); the pairing key never reaches the host.
    nonce, pairing = hello.get('nonce'), hello.get('pairing_id')
    try:
        try:
            sock.sendall(encode({'type': 'hello', 'protocol': PROTOCOL, 'token': token, 'origin': origin,
                                 'host': {'version': HOST_VERSION},
                                 'nonce': nonce if _hex(nonce, 64) else None,
                                 'pairing_id': pairing if _hex(pairing, 32) else None,
                                 'extension': {'version': _text(extension.get('version'), 32),
                                               'chrome': _text(extension.get('chrome'), 64)}}, 'little'))
            welcome = read_message(sock, 'little')
        except (OSError, Ended):
            welcome = None
        if welcome is None or welcome.get('type') != 'welcome':
            write_message(stdout, {'type': 'status', 'state': 'refused', 'message': REFUSED})
            return EXIT_PROTOCOL
        sock.settimeout(None)
        write_message(stdout, welcome)
        outcome = {}

        def upstream():
            try:
                _pump(stdin, sock, sys.byteorder, 'little', _from_extension)
            except (Ended, OSError, ValueError) as exc:
                outcome['up'] = getattr(exc, 'code', 'io')
            finally:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        thread = threading.Thread(target=upstream, name='pebble-host-upstream', daemon=True)
        thread.start()
        try:
            _pump(sock, stdout, 'little', sys.byteorder, lambda message: None)
        except (Ended, OSError, ValueError) as exc:
            outcome['down'] = getattr(exc, 'code', 'io')
        return EXIT_PROTOCOL if outcome else EXIT_OK
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


if __name__ == '__main__':
    sys.exit(main())
