"""Phase 46 (1.9.29, D214): the WebSocket the iPhone bridge speaks (RFC 6455, the server's side) over asyncio streams.

Only what contract PEBBLE_REMOTE_BRIDGE 3.1 uses: the upgrade at /pebble-remote/v1 choosing the subprotocol
pebble-remote.v1, text messages of UTF-8 JSON, no extension (so no compression), pings answered with pongs, and the
closing handshake. A frame's length is read and checked before its bytes are, so a message over the limit is refused
(1009) unread. Every protocol error closes with its code: 1002 for framing, 1003 for a binary message, 1007 for text
that is not UTF-8, 1008 for a message in more than 64 pieces, 1009 for size.

Phase 46 (1.9.29, w27's review F2): nothing here waits for a peer for ever. A write waits only while the peer reads
(one that reads nothing for 45 s is cut off), and a close gives up after its two seconds even when the socket is full.
"""

import asyncio
import base64
import binascii
import hashlib
import time

PATH, SUBPROTOCOL = '/pebble-remote/v1', 'pebble-remote.v1'
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
HEAD_LIMIT = 16384
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
NORMAL, GOING_AWAY, PROTOCOL, UNSUPPORTED, NO_STATUS, ABNORMAL, BAD_DATA, POLICY, TOO_BIG, TRY_LATER = (
    1000, 1001, 1002, 1003, 1005, 1006, 1007, 1008, 1009, 1013)
MAX_FRAGMENTS = 64       # F8: the pieces of one message (an empty piece adds nothing to its size, but costs a read)
STALLED_SECONDS = 45.0   # F2: a peer that reads none of what was written to it for this long is cut off


class ProtocolError(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode('ascii')).digest()).decode('ascii')


def unmask(data, mask):
    size = len(data)
    if not size:
        return b''
    return (int.from_bytes(data, 'big') ^ int.from_bytes((mask * (size // 4 + 1))[:size], 'big')).to_bytes(size, 'big')


def frame(opcode, payload=b''):
    """One final, unmasked frame, as a server sends them."""
    size = len(payload)
    if size < 126:
        head = bytes((0x80 | opcode, size))
    elif size < 65536:
        head = bytes((0x80 | opcode, 126)) + size.to_bytes(2, 'big')
    else:
        head = bytes((0x80 | opcode, 127)) + size.to_bytes(8, 'big')
    return head + payload


def valid_close_code(code):
    return code in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014) or 3000 <= code <= 4999


async def answer(writer, status, extra=''):
    """An HTTP refusal before any WebSocket (a bad upgrade, or C6's 429), then the close."""
    try:
        writer.write(('HTTP/1.1 ' + status + '\r\nContent-Length: 0\r\nConnection: close\r\n' + extra + '\r\n')
                     .encode('latin-1'))
        await asyncio.wait_for(writer.drain(), 5)
    except (ConnectionError, OSError, asyncio.TimeoutError):
        pass
    finally:
        writer.close()


def _tokens(headers, name):
    return [token.strip().lower() for value in headers.get(name, ()) for token in value.split(',') if token.strip()]


async def upgrade(reader, writer, max_message, timeout=10.0):
    """Read the HTTP upgrade and answer it: a Socket, or None once the request was refused (or was not HTTP)."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError, OSError,
            ValueError):
        writer.close()
        return None
    lines = head.decode('latin-1').split('\r\n')
    request, headers = lines[0].split(' '), {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(':')
        if not sep or not name or name != name.strip():
            return await answer(writer, '400 Bad Request')
        headers.setdefault(name.lower(), []).append(value.strip())
    if len(request) != 3 or request[0] != 'GET' or request[2] != 'HTTP/1.1':
        return await answer(writer, '400 Bad Request')
    if request[1] != PATH:
        return await answer(writer, '404 Not Found')
    if 'websocket' not in _tokens(headers, 'upgrade') or 'upgrade' not in _tokens(headers, 'connection'):
        return await answer(writer, '400 Bad Request')
    if _tokens(headers, 'sec-websocket-version') != ['13']:
        return await answer(writer, '426 Upgrade Required', 'Sec-WebSocket-Version: 13\r\n')
    keys = headers.get('sec-websocket-key', [])
    try:
        if len(keys) != 1 or len(base64.b64decode(keys[0], validate=True)) != 16:
            raise ValueError
    except (ValueError, binascii.Error):
        return await answer(writer, '400 Bad Request')
    if SUBPROTOCOL not in _tokens(headers, 'sec-websocket-protocol'):
        return await answer(writer, '400 Bad Request')
    writer.write(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                  'Sec-WebSocket-Accept: ' + accept_key(keys[0]) + '\r\nSec-WebSocket-Protocol: ' + SUBPROTOCOL
                  + '\r\n\r\n').encode('latin-1'))
    try:
        await asyncio.wait_for(writer.drain(), timeout)
    except (ConnectionError, OSError, asyncio.TimeoutError):
        writer.close()
        return None
    return Socket(reader, writer, max_message)


class Socket:
    """One upgraded connection. recv() gives the next text message, or None once the connection is over; close_code
    then says why (the peer's close code, 1006 for a dropped connection, or ours after a protocol error). Pings are
    answered inside recv(), so one task keeps calling it."""

    def __init__(self, reader, writer, max_message):
        self.reader, self.writer, self.max_message = reader, writer, max_message
        self.last_received = time.monotonic()
        self.close_sent = self.closed = False
        self.close_code = None
        self._lock = asyncio.Lock()
        self._done = asyncio.Event()

    async def send(self, opcode, payload=b''):
        if self.closed or (self.close_sent and opcode != OP_CLOSE):
            raise ConnectionError('the WebSocket is closing')
        async with self._lock:
            if self.closed:  # F2: cut off while this frame waited its turn (its transport is gone)
                raise ConnectionError('the WebSocket is closed')
            self.writer.write(frame(opcode, payload))
            await self._drain()

    async def _drain(self):
        """F2: wait while the peer reads what was written, however slowly; a peer that has read none of it for
        STALLED_SECONDS is cut off, so neither this write nor a close queued behind it waits for ever."""
        transport, left, since = self.writer.transport, None, time.monotonic()
        while True:
            try:
                await asyncio.wait_for(self.writer.drain(), 1.0)
                return
            except asyncio.TimeoutError:
                if transport.is_closing():
                    raise ConnectionError('the WebSocket is closed') from None
                waiting = transport.get_write_buffer_size()
                if left is None or waiting < left:
                    left, since = waiting, time.monotonic()
                elif time.monotonic() - since > STALLED_SECONDS:
                    self.abort()
                    raise ConnectionError('the peer stopped reading') from None

    async def send_text(self, text):
        await self.send(OP_TEXT, text.encode('utf-8'))

    async def close(self, code=NORMAL, reason='', timeout=2.0):
        """Start the closing handshake, then wait for the peer's close (read by recv()) or drop the connection."""
        await self._send_close(code, reason, timeout)
        if not self.closed:
            try:
                await asyncio.wait_for(self._done.wait(), timeout)
            except asyncio.TimeoutError:
                self.abort()

    def abort(self):
        try:
            self.writer.transport.abort()
        except Exception:
            pass
        self._finish(ABNORMAL, aborted=True)

    async def wait_closed(self):
        await self._done.wait()

    async def _send_close(self, code, reason='', timeout=2.0):
        if self.close_sent or self.closed:
            return
        self.close_sent = True
        if self.close_code is None:
            self.close_code = code
        try:  # F2: bounded, since behind a full socket (or a pong waiting for one) it waits for the peer to read
            await asyncio.wait_for(self.send(OP_CLOSE, code.to_bytes(2, 'big') + reason.encode('utf-8')[:123]), timeout)
        except asyncio.TimeoutError:
            self.abort()
        except (ConnectionError, OSError, RuntimeError):
            self._finish()

    def _finish(self, code=None, aborted=False):
        if self.close_code is None:
            self.close_code = ABNORMAL if code is None else code
        if not self.closed:
            self.closed = True
            # An aborted TLS transport closed again forgets its protocol (CPython's sslproto), and the reader, still
            # draining what arrived before, then fails to resume it (w27's review, F2's AttributeError).
            if not aborted:
                try:
                    self.writer.close()
                except Exception:
                    pass
        self._done.set()

    async def _frame(self):
        head = await self.reader.readexactly(2)
        fin, opcode, size = head[0] & 0x80, head[0] & 0x0F, head[1] & 0x7F
        if head[0] & 0x70:
            raise ProtocolError(PROTOCOL, 'reserved bits set')
        if opcode not in (OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG):
            raise ProtocolError(PROTOCOL, 'unknown opcode')
        if not head[1] & 0x80:
            raise ProtocolError(PROTOCOL, 'client frames must be masked')
        if size == 126:
            size = int.from_bytes(await self.reader.readexactly(2), 'big')
            if size < 126:
                raise ProtocolError(PROTOCOL, 'non-minimal length')
        elif size == 127:
            size = int.from_bytes(await self.reader.readexactly(8), 'big')
            if size < 65536 or size >> 63:
                raise ProtocolError(PROTOCOL, 'bad length')
        if opcode >= OP_CLOSE and (not fin or size > 125):
            raise ProtocolError(PROTOCOL, 'bad control frame')
        if opcode == OP_BINARY:
            raise ProtocolError(UNSUPPORTED, 'text messages only')
        if size > self.max_message:
            raise ProtocolError(TOO_BIG, 'message too big')
        mask = await self.reader.readexactly(4)
        payload = unmask(await self.reader.readexactly(size), mask)
        self.last_received = time.monotonic()
        return bool(fin), opcode, payload

    async def recv(self):
        parts, size = None, 0
        try:
            while not self.closed:  # frames still buffered when the connection was cut are not handled
                fin, opcode, payload = await self._frame()
                if opcode == OP_PING:
                    if not self.close_sent:
                        await self.send(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    continue
                if opcode == OP_CLOSE:
                    await self._closed_by_peer(payload)
                    return None
                if opcode == OP_CONT and parts is None:
                    raise ProtocolError(PROTOCOL, 'a continuation without a message')
                if opcode == OP_TEXT:
                    if parts is not None:
                        raise ProtocolError(PROTOCOL, 'a new message before the last one ended')
                    parts, size = [], 0
                parts.append(payload)
                size += len(payload)
                if size > self.max_message:
                    raise ProtocolError(TOO_BIG, 'message too big')
                if len(parts) > MAX_FRAGMENTS:
                    raise ProtocolError(POLICY, 'a message in too many pieces')
                if fin:
                    data, parts = b''.join(parts), None
                    try:
                        return data.decode('utf-8')
                    except UnicodeDecodeError:
                        raise ProtocolError(BAD_DATA, 'text that is not UTF-8') from None
            return None
        except ProtocolError as exc:
            if self.close_code is None:
                self.close_code = exc.code
            await self._send_close(exc.code, exc.reason)
            self._finish()
            return None
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self._finish(ABNORMAL)
            return None

    async def _closed_by_peer(self, payload):
        code, reply = NO_STATUS, NORMAL
        if len(payload) == 1:
            code = reply = PROTOCOL
        elif len(payload) >= 2:
            code = int.from_bytes(payload[:2], 'big')
            try:
                payload[2:].decode('utf-8')
            except UnicodeDecodeError:
                reply = BAD_DATA
            if not valid_close_code(code):
                reply = PROTOCOL
            elif reply == NORMAL:
                reply = code
        if self.close_code is None:
            self.close_code = code
        await self._send_close(reply)
        self._finish()
