"""The Responses API's WebSocket mode, client side (Phase 46, D154: speed on the ChatGPT route).

OpenAI's WebSocket mode (https://developers.openai.com/api/docs/guides/websocket-mode) keeps one connection to
/v1/responses open for a run. Each turn is one `response.create` message whose fields are the HTTP request body's
(without `stream`); the events come back as text messages in the HTTP stream's event shapes, the turn ending at its
terminal event. A turn may continue the connection's latest response with `previous_response_id` and only the new
input items: the service keeps that response in the connection's memory (with store=false nothing is persisted, and an
evicted id fails with `previous_response_not_found`). A connection lasts at most 60 minutes.

Measured before this module (golden dev d-cb-01 on gpt-6.1-sol, 1.9.18): every HTTP request waited 2.7-4.0 s for its
response headers and a turn with under 100 output tokens took 7.5-9 s, while Codex, whose binary carries this mode's
request types, finished comparable turns in 2.6-3.6 s; a TLS connection to the API takes about 0.03 s here.

RFC 6455, client side only: no extensions are offered, every client frame is masked, server frames must not be;
text, continuation, ping, pong and close frames; a binary message is refused. Nothing here reads credentials: the
caller passes the request's headers, and they are written to the socket and kept nowhere."""

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'  # RFC 6455 section 1.3 (measured 21:03: the old value failed every upgrade)
MAX_HEAD = 16_384           # the upgrade response's status line and headers
MAX_REFUSAL = 8_192         # a refused upgrade's body, read for its error message
MAX_MESSAGE = 2_000_000     # one event, as one SSE line (responses._stream_pass)
LIFETIME = 50 * 60.0        # seconds a connection is reused (the service closes it at 60 minutes)
IDLE = 90.0                 # seconds a connection may sit unused and still be reused (a dropped path is not
                            # noticed until a read times out, so long-idle connections are opened afresh)
POOL = 4                    # open connections kept at most (one per prompt_cache_key: a conversation's)
DROPPED_HEADERS = {'host', 'upgrade', 'connection', 'content-type', 'content-length', 'accept',
                   'sec-websocket-key', 'sec-websocket-version', 'sec-websocket-extensions'}


class SocketRefused(Exception):
    """The service answered the upgrade with another status than 101 (`status`, and its error body's text)."""

    def __init__(self, status, body=b''):
        super().__init__(f'WebSocket upgrade refused with HTTP {status}.')
        self.status, self.body = status, body


class SocketClosed(ConnectionError):
    """The connection ended: a close frame (`code`), the peer's end of stream, or a protocol error. A ConnectionError,
    as a dropped HTTP stream's is, so recovery.transient retries the turn (on a new connection, with the whole input)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _accept(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode('ascii')).digest()).decode('ascii')


def _mask(data, mask):
    """XOR with the repeating 4-byte mask, at C speed (a request body can be hundreds of kilobytes)."""
    if not data:
        return b''
    size = len(data)
    repeated = (mask * (size // 4 + 1))[:size]
    return (int.from_bytes(data, 'big') ^ int.from_bytes(repeated, 'big')).to_bytes(size, 'big')


class ResponsesSocket:
    """One upgraded connection. `response_id` and `chain` describe the response a next turn may continue: chain is the
    full input that turn would start with (the input sent, then its output as Pebble replays it). Any failure or
    interruption of a turn leaves both None and the connection `broken`, so it is never reused mid-response."""

    def __init__(self, sock, clock=time.monotonic):
        self.sock, self.clock = sock, clock
        self.opened = self.used = clock()
        self.response_id = self.chain = None
        self.broken = self.busy = False
        self._buffer = bytearray()
        self._send_lock = threading.Lock()

    @classmethod
    def handshake(cls, sock, host, path, headers, clock=time.monotonic):
        """Upgrade `sock` (connected, TLS done) to a WebSocket at `path`, sending `headers` (the request's own, minus
        the HTTP body's) with the upgrade. Raises SocketRefused for any status but 101, SocketClosed for a malformed
        answer."""
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        lines = [f'GET {path} HTTP/1.1', f'Host: {host}', 'Upgrade: websocket', 'Connection: Upgrade',
                 f'Sec-WebSocket-Key: {key}', 'Sec-WebSocket-Version: 13']
        for name, value in headers.items():
            if name.lower() in DROPPED_HEADERS or any(c in str(name) + str(value) for c in '\r\n'):
                continue
            lines.append(f'{name}: {value}')
        sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('utf-8'))
        conn = cls(sock, clock)
        head = conn._read_head()
        status_line, *fields = head.decode('latin-1').split('\r\n')
        parts = status_line.split(' ', 2)
        if len(parts) < 2 or not parts[0].startswith('HTTP/1.') or not parts[1].isdigit():
            raise SocketClosed('The model service sent an unreadable upgrade answer.')
        found = {}
        for field in fields:
            name, sep, value = field.partition(':')
            if sep:
                found[name.strip().lower()] = value.strip()
        status = int(parts[1])
        if status != 101:
            raise SocketRefused(status, conn._refusal_body(found))
        if found.get('upgrade', '').lower() != 'websocket' or 'upgrade' not in found.get('connection', '').lower():
            raise SocketClosed('The model service did not switch to a WebSocket.')
        if found.get('sec-websocket-accept') != _accept(key):
            raise SocketClosed("The model service's WebSocket answer did not match this request.")
        if found.get('sec-websocket-extensions'):
            raise SocketClosed('The model service chose a WebSocket extension Pebble did not offer.')
        return conn

    def _read_head(self):
        while b'\r\n\r\n' not in self._buffer:
            if len(self._buffer) > MAX_HEAD:
                raise SocketClosed('The model service sent an oversized upgrade answer.')
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SocketClosed('The model service closed the connection during the upgrade.')
            self._buffer += chunk
        end = self._buffer.index(b'\r\n\r\n')
        head = bytes(self._buffer[:end])
        del self._buffer[:end + 4]
        return head

    def _refusal_body(self, headers):
        """Up to MAX_REFUSAL bytes of a refused upgrade's body (its error JSON), never more than it declared."""
        try:
            length = min(int(headers.get('content-length', '0')), MAX_REFUSAL)
        except ValueError:
            length = 0
        try:
            while len(self._buffer) < length:
                chunk = self.sock.recv(length - len(self._buffer))
                if not chunk:
                    break
                self._buffer += chunk
        except OSError:
            pass
        return bytes(self._buffer[:length])

    # -- frames ------------------------------------------------------------------------------------------------------

    def _send(self, opcode, data):
        header = bytearray([0x80 | opcode])
        size = len(data)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack('!H', size)
        else:
            header += bytes([0x80 | 127]) + struct.pack('!Q', size)
        mask = os.urandom(4)
        with self._send_lock:
            self.sock.sendall(bytes(header) + mask + _mask(data, mask))

    def send_json(self, payload):
        self._send(0x1, json.dumps(payload).encode('utf-8'))

    def _read(self, size):
        while len(self._buffer) < size:
            chunk = self.sock.recv(max(65536, size - len(self._buffer)))
            if not chunk:
                raise SocketClosed('The model service closed the connection.')
            self._buffer += chunk
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def receive(self):
        """The next text message, decoded. Pings are answered and pongs skipped; a close frame raises SocketClosed."""
        parts, size, kind = [], 0, None
        while True:
            first, second = self._read(2)
            final, opcode = first & 0x80, first & 0x0F
            if first & 0x70 or second & 0x80:
                raise SocketClosed('The model service sent a WebSocket frame Pebble cannot read.')
            length = second & 0x7F
            if length == 126:
                length = struct.unpack('!H', self._read(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._read(8))[0]
            if opcode >= 0x8:
                if length > 125 or not final:
                    raise SocketClosed('The model service sent a malformed WebSocket control frame.')
                data = self._read(length)
                if opcode == 0x9:
                    self._send(0xA, data)
                    continue
                if opcode == 0xA:
                    continue
                if opcode == 0x8:
                    code = struct.unpack('!H', data[:2])[0] if len(data) >= 2 else None
                    self.broken = True
                    try:
                        self._send(0x8, data[:2])
                    except OSError:
                        pass
                    raise SocketClosed('The model service closed the connection'
                                       + (f' (code {code}).' if code else '.'), code)
                raise SocketClosed('The model service sent an unknown WebSocket control frame.')
            if size + length > MAX_MESSAGE:
                raise ValueError('Provider stream exceeds the response limit.')
            if opcode == 0x0:
                if kind is None:
                    raise SocketClosed('The model service continued a WebSocket message that never began.')
            elif opcode in (0x1, 0x2):
                if kind is not None:
                    raise SocketClosed('The model service began a WebSocket message inside another.')
                kind = opcode
            else:
                raise SocketClosed('The model service sent an unknown WebSocket frame.')
            parts.append(self._read(length))
            size += length
            if final:
                if kind != 0x1:
                    raise SocketClosed('The model service sent a binary WebSocket message.')
                try:
                    return b''.join(parts).decode('utf-8')
                except UnicodeDecodeError:
                    raise SocketClosed('The model service sent a WebSocket message that is not UTF-8.') from None

    # -- life ------------------------------------------------------------------------------------------------------

    def settimeout(self, seconds):
        self.sock.settimeout(seconds)

    def reusable(self, now=None):
        now = self.clock() if now is None else now
        return not self.broken and not self.busy and now - self.opened < LIFETIME and now - self.used < IDLE

    def abort(self):
        """Unblock a read from another thread (the watchdog's): the socket's shutdown, as responses._abort does."""
        self.broken = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self):
        self.broken = True
        try:
            self._send(0x8, struct.pack('!H', 1000))  # a fresh random mask, as RFC 6455 asks of every client frame
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class SocketPool:
    """The root provider's open connections, one per conversation (its prompt_cache_key), at most POOL; one is taken
    for one turn (busy) and given back only after a clean turn. Thread-safe."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._sockets = {}

    def take(self, key):
        """The reusable connection kept for `key`, marked busy, or None. Stale connections are closed on the way."""
        with self._lock:
            now, stale = self.clock(), []
            for name, conn in list(self._sockets.items()):
                if conn.broken or now - conn.opened >= LIFETIME or (not conn.busy and now - conn.used >= IDLE):
                    stale.append(self._sockets.pop(name))
            conn = self._sockets.get(key)
            if conn is not None and conn.reusable(now):
                conn.busy = True
            else:
                conn = None
        for old in stale:
            old.close()
        return conn

    def keep(self, key, conn):
        """Give `conn` back after a clean turn (or keep a new one), closing the least recently used beyond POOL."""
        with self._lock:
            conn.busy, conn.used = False, self.clock()
            if conn.broken:
                self._sockets.pop(key, None)
                evicted = []
            else:
                replaced = self._sockets.get(key)
                self._sockets[key] = conn
                evicted = [replaced] if replaced is not None and replaced is not conn else []
                while len(self._sockets) > POOL:
                    name = min(self._sockets, key=lambda k: self._sockets[k].used)
                    evicted.append(self._sockets.pop(name))
        for old in evicted:
            old.close()

    def drop(self, key, conn):
        with self._lock:
            if self._sockets.get(key) is conn:
                del self._sockets[key]
        conn.close()

    def close(self):
        with self._lock:
            sockets, self._sockets = list(self._sockets.values()), {}
        for conn in sockets:
            conn.close()
