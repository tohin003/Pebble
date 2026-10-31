"""Phase 46 (1.9.29, D214, D215): the iPhone bridge, inside Pebble's backend (claude-w's answer of 2026-10-30 10:49).

One asyncio loop on a thread of its own, running only while the person has Remote on (remote.py starts and stops it).
It serves contract PEBBLE_REMOTE_BRIDGE v0.3 with proposal v0.5 to paired phones:

- transport (3.1, C6): TLS 1.3 with the pinned P-256 key, WebSocket with the subprotocol pebble-remote.v1,
  TCP_NODELAY, a connection silent for 45 s closed. Listeners on loopback, the LAN's private addresses (unless the
  person turned LAN listening off) and Tailscale's 100.x address, made again when the addresses change; at most 10
  handshakes a minute from one address (loopback exempt, v0.3 item 13); Bonjour named by the bridge id.
- pairing (3.2, X-1): a link valid 5 minutes and once, its code compared in constant time with at most 5 tries, both
  signatures, then six digits the person confirms on the Mac; nothing is stored before that.
- every connection (3.3): challenge, a hello signed over this connection's nonce and the pin, welcome, and the events a
  resumed phone missed, in order, before any live one. One connection per device.
- requests (3.4 to 3.9, 3.11, C1, C2, S-05, v0.5): request ids, the allowlist, Face ID proofs bound to the connection
  and the call, C2's replay memory keyed by (device, r), the unlock, idempotency, size and rate limits, then Pebble's
  own dispatch, exactly as the main window's calls reach it.
- events (3.5, C4): every event the main window gets, token and reasoning text merged within 30 ms, a replay buffer
  of at least 5,000 events or 10 minutes, and 4 MB of backpressure.

Phase 46 (1.9.29, w27's adversarial review, F1 to F11). Every socket counts from the moment it is accepted, before any
TLS, against a share of the process's open files, a cap per address and a cap on sockets that are not yet a phone;
over a cap the oldest socket that is not yet a phone gives way, so idle sockets can neither use up Pebble's files nor
keep a phone out (F1, F2). Only the device's current connection speaks for it: what a closing or replaced connection
still sends unlocks nothing and runs nothing (F3), and a removed phone's calls that still wait for a thread never reach
Pebble (F4). Reads are never kept under a retry key, and kept answers are bounded in bytes (F7); a retry after a
timeout learns what Pebble did (F11).

Logs are content-free: connection numbers, method names, codes and timings; never a parameter, a key, a code or a name.
"""

import asyncio
import errno
import hmac
import json
import logging
import os
import re
import resource
import socket
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor

from . import remote_keys as keys
from . import remote_mac as mac
from . import remote_policy as policy
from . import remote_ws as ws

log = logging.getLogger('pebble.remote')

MAX_REQUEST = 131072          # a request's UTF-8 bytes (welcome.limits.max_request; the shell's own limit)
MAX_PARAMS = 128000           # the shell's limit on a call's params as JSON (main.rs rpc)
MAX_FRAME = 8 << 20           # the largest answer (welcome.limits.max_frame)
HARD_MESSAGE = 1 << 20        # a message above this is refused unread (1009)
EARLY_MESSAGE = 16384         # F8: the same before welcome (pair and hello are under 1 KB)
MAX_EVENT = 1 << 20           # an event above this is trimmed to its ids (3.9)
MAX_NESTING = 64              # deeper JSON is unreadable both ways (security review X-5, S-02)
MAX_QUEUE = 4 << 20           # 3.5's backpressure: more queued for one phone drops its connection
RATE_BURST, RATE_PER_SECOND = 20, 10
CALL_SECONDS = 30.0
PAIRING_SECONDS, PAIRING_TRIES, CONFIRM_SECONDS = 300, 5, 120
IDEM_SECONDS, IDEM_LIMIT = 600, 20000
# F7: answers kept under retry keys, in bytes: one answer, one device's, and all of them. Past these, a retry learns
# only that the call happened (ok, code and words; a result too large to keep is null).
IDEM_ANSWER, IDEM_DEVICE, IDEM_BYTES = 256 << 10, 4 << 20, 16 << 20
REPLAY_EVENTS, REPLAY_SECONDS, REPLAY_BYTES, REPLAY_HARD = 5000, 600, 32 << 20, 100000
BATCH_SECONDS = 0.030
PROOF_WINDOW_MS, PROOF_MEMORY_SECONDS = 60000, 120
IDLE_SECONDS, AUTH_SECONDS = 45.0, 20.0
HANDSHAKE_SECONDS = 10.0      # F1: TLS and the WebSocket upgrade, from the moment the socket is accepted
CLOSE_SECONDS, CLOSING_SECONDS = 2.0, 5.0  # F2: a TLS close, and a connection's whole close, before they are cut
MAX_CONNECTIONS, MAX_UNAUTHENTICATED = 32, 8
# F1: sockets, counted from accept. The bridge holds at most an eighth of the process's open files (launchd gives an
# app 256), between 8 and 64; one address at most 4 (this Mac's own processes, on loopback, 8).
SOCKETS_MOST, SOCKETS_LEAST, SOCKETS_SHARE = 64, 8, 8
PER_ADDRESS, PER_LOOPBACK = 4, 8
LOOPBACK = ('127.0.0.1', '::1')
LISTEN_BACKLOG, ACCEPT_BATCH = 64, 16
HANDSHAKES_PER_MINUTE = 10
NETWORK_SECONDS, MAC_SECONDS = 10.0, 60.0
STOPPING = ('cancel', 'run_pause')  # V1-011: stopping never waits behind slow calls

# Every sentence the bridge writes is for the person (4.7), in Pebble's voice.
ERRORS = {
    'not_allowed': "Pebble can't do that from your iPhone. Use Pebble on your Mac for it.",
    'proof_required': 'Confirm with Face ID to do this.',
    'bad_proof': "Pebble couldn't check that Face ID confirmation. Try again.",
    'too_large': "That's too much to send at once.",
    'rate_limited': "That's a lot of requests at once. Try again in a moment.",
    'pebble_offline': "Pebble isn't running on your Mac.",
    'timeout': "Pebble didn't answer in time.",
    'bad_request': "Pebble couldn't read that request. Update Pebble Remote if this keeps happening.",
    'unlock_required': 'Unlock Pebble Remote with Face ID before Pebble acts on what you ask.',
}
ID_REFUSED = 'Pebble Remote sent a request it could not name. Update Pebble Remote if this keeps happening.'
RETRY_KEY_REFUSED = 'Pebble Remote must send this with a retry key. Update Pebble Remote if this keeps happening.'
WORKSPACE_REFUSED = "Choose a folder from the list. Pebble Remote can't connect that one."
PAIR_REFUSED = "Pairing wasn't confirmed on your Mac. Show a new pairing link there and try again."
BYE = {
    'unknown_device': "This iPhone isn't paired with this Mac. Pair it again from Pebble's settings on the Mac.",
    'revoked': 'This iPhone was removed from Pebble on your Mac. Pair it again to keep using it.',
    'bad_signature': "This iPhone's key didn't match. Pair it again from Pebble's settings on the Mac.",
    'bad_version': "This Pebble Remote doesn't match Pebble on your Mac. Update both to the latest version.",
    'replaced': 'Pebble Remote connected again from this iPhone.',
    'busy': 'Your Mac is busy. Try again in a moment.',
    'bad_code': 'This pairing link has expired or was already used. Show a new one on your Mac.',
    'too_large': 'That message was too large to read, so Pebble closed the connection. Pebble Remote reconnects by '
                 'itself.',
}
# main.py's answer to a defect (Phase 45, F239): one sentence for the person, the traceback in Pebble's log.
DEFECTS = (TypeError, KeyError, AttributeError, IndexError, NameError)
DEFECT_TEXT = "Pebble couldn't do that because of an internal error. It is recorded in Pebble's log."
ERROR_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')
NESTING = re.compile(r'"(?:[^"\\]|\\.)*"|[\[\]{}]')


def too_deep(text, limit=MAX_NESTING):
    """Nested deeper than `limit` (brackets outside strings counted, so a parser never recurses that far)."""
    if text.count('[') + text.count('{') <= limit:
        return False
    depth = 0
    for match in NESTING.finditer(text):
        token = match.group()
        if token in ('[', '{'):
            depth += 1
            if depth > limit:
                return True
        elif token in (']', '}'):
            depth -= 1
    return False


def _no_constant(name):
    raise ValueError('not JSON: ' + name)


def parse(text):
    """A frame from the phone as JSON, or None: unreadable, NaN or Infinity, or nested too deeply (S-02, M-1)."""
    if too_deep(text):
        return None
    try:
        return json.loads(text, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return None


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def clip(value, depth=0, limit=MAX_NESTING - 4):
    """An answer the phone can read (X-5): what lies deeper than `limit` becomes a short note. Only the rare answer
    that is too deep is walked; Pebble's own answers nest about ten levels."""
    if isinstance(value, (dict, list)) and depth >= limit:
        return '(nested too deeply to show here)'
    if isinstance(value, dict):
        return {key: clip(item, depth + 1, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [clip(item, depth + 1, limit) for item in value]
    return value


def short(identifier):
    return (identifier or '-')[:6]


def quiet(loop, context):
    """asyncio's own reports on the bridge's loop (a TLS handshake a scanner broke off, a reset connection): one
    content-free line. The default handler would write the peer's address and the transport into Pebble's log."""
    exception = context.get('exception')
    if exception is None or isinstance(exception, (OSError, ConnectionError, asyncio.IncompleteReadError,
                                                   asyncio.TimeoutError)):
        log.info('Remote connection ended early (%s)', type(exception).__name__ if exception else 'no detail')
    else:
        log.warning('Remote bridge error (%s)', type(exception).__name__,
                    exc_info=(type(exception), exception, exception.__traceback__))


def label(method):
    return method if isinstance(method, str) and method in policy.METHOD_SET else '?'


def socket_budget():
    """F1: the sockets the bridge may hold, a small share of this process's open-file limit, so Pebble's own files,
    pipes and model connections always keep the rest."""
    soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    if soft == resource.RLIM_INFINITY or soft <= 0:
        return SOCKETS_MOST
    return max(SOCKETS_LEAST, min(SOCKETS_MOST, soft // SOCKETS_SHARE))


class Removed(ValueError):
    """F4: a call of a phone removed while the call waited for a thread."""


class Bucket:
    """3.9: 20 requests a second in a burst, 10 a second sustained."""

    def __init__(self):
        self.tokens, self.at = float(RATE_BURST), time.monotonic()

    def take(self):
        now = time.monotonic()
        self.tokens = min(RATE_BURST, self.tokens + (now - self.at) * RATE_PER_SECOND)
        self.at = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class Conn:
    """One phone connection. Everything sent goes through one queue and one writer task, so frames reach the wire in
    the order they were queued (welcome, replayed events, then live ones)."""

    def __init__(self, number, socket_, local):
        self.number, self.ws, self.local = number, socket_, local
        self.nonce = keys.b64u(os.urandom(32))
        self.device = self.writer = self.closing = self.pairing = None
        self.live = self.confirming = False
        self.used = {}                  # request id -> when (kept PROOF_MEMORY_SECONDS: 3.6's unused id)
        self.queue, self.queued = deque(), 0
        self.wake = asyncio.Event()
        self.opened, self.closing_at = time.monotonic(), None

    def send(self, data):
        if self.closing is not None:
            return
        if isinstance(data, str):
            data = data.encode('utf-8')
        self.queue.append(data)
        self.queued += len(data)
        # What waits behind the next frame out counts, so one answer up to max_frame (8 MB, 3.9) still goes (F5).
        if self.queued - len(self.queue[0]) > MAX_QUEUE:
            # The phone is not reading: a close frame would wait behind the same full socket, so drop the connection.
            # The phone reconnects and resumes from its last seq (3.5).
            log.info('Remote conn#%d: more than 4 MB queued; connection dropped', self.number)
            self.queue.clear()
            self.queued = 0
            self.closing, self.live = (ws.TRY_LATER, 'too far behind'), False
            self.ws.abort()
        self.wake.set()

    def bye(self, code, error=None):
        self.live = False
        self.send(dumps({'t': 'bye', 'code': code, 'error': error or BYE.get(code, '')}))
        self.close(ws.NORMAL if code in ('replaced', 'revoked') else ws.TOO_BIG if code == 'too_large' else ws.POLICY,
                   code)

    def close(self, code=ws.NORMAL, reason=''):
        self.live = False
        if self.closing is None:
            self.closing, self.closing_at = (code, reason), time.monotonic()
            self.wake.set()

    async def write(self):
        try:
            while True:
                while self.queue:
                    data = self.queue.popleft()
                    self.queued -= len(data)
                    await self.ws.send(ws.OP_TEXT, data)
                if self.closing is not None:
                    await self.ws.close(*self.closing)
                    return
                self.wake.clear()
                await self.wake.wait()
        except (ConnectionError, OSError, RuntimeError):
            self.ws.abort()


class Accepted:
    """F1: one accepted socket, counted from accept until its handler ends; by then its file is closed (or being cut)."""

    def __init__(self, address, host, raw):
        self.address, self.host, self.raw = address, host, raw
        self.at = time.monotonic()
        self.task = self.writer = self.conn = None
        self.started = self.dropped = False

    def early(self):
        """Not yet a phone (no welcome) and not pairing: it gives way to a newer socket over a cap."""
        return self.conn is None or (self.conn.device is None and not self.conn.confirming)


class Listener:
    """F1: one address's listening socket. Each connection is taken here and counted (Bridge.take) before any TLS;
    asyncio's own servers hold every socket through its TLS handshake, uncounted. `sockets` as asyncio's Server names
    them."""

    def __init__(self, bridge, host, port):
        self.bridge, self.host, self.paused = bridge, host, None
        sock = socket.create_server((host, port), backlog=LISTEN_BACKLOG)
        sock.setblocking(False)
        self.sockets = [sock]
        bridge.loop.add_reader(sock.fileno(), self.ready)

    def ready(self):
        for _ in range(ACCEPT_BATCH):  # then the loop serves its other connections
            try:
                raw, peer = self.sockets[0].accept()
            except (BlockingIOError, InterruptedError, IndexError):
                return
            except ConnectionAbortedError:
                continue
            except OSError as exc:
                if exc.errno in (errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM):
                    log.warning('Remote bridge is out of files; it takes no connection for a second')
                    self.bridge.loop.remove_reader(self.sockets[0].fileno())
                    self.paused = self.bridge.loop.call_later(1.0, self.resume)
                return
            self.bridge.take(raw, peer[0], self.host)

    def resume(self):
        self.paused = None
        if self.sockets:
            self.bridge.loop.add_reader(self.sockets[0].fileno(), self.ready)

    def close(self):
        if self.paused is not None:
            self.paused.cancel()
        if self.sockets:
            sock = self.sockets.pop()
            self.bridge.loop.remove_reader(sock.fileno())
            sock.close()


class Bridge:
    """The bridge's state, owned by its loop's thread. `host` is Pebble's side (remote.RemoteHub): dispatch, the
    activity record, the Mac's settings and its Settings page. Other threads reach the bridge only through tee() and
    the loop (call_soon_threadsafe, run_coroutine_threadsafe); `snapshot` is a fresh dict each time, read without
    locks."""

    def __init__(self, host, identity, registry, *, bid, name, port, lan, unlock_seconds, folders, version,
                 link_loopback=False, address_plan=None):
        self.host, self.identity, self.registry = host, identity, registry
        self.bid, self.pin, self.name, self.port = bid, identity.pin, name, port
        self.lan, self.unlock_seconds, self.folders, self.version = lan, unlock_seconds, folders, version
        self.link_loopback = link_loopback
        self.address_plan = address_plan or mac.address_plan
        self.epoch = 'e-' + keys.b64u(os.urandom(9))
        self.seq, self.buffer, self.buffer_bytes = 0, deque(), 0
        self.conns, self.by_device = set(), {}
        self.counter = 0
        self.accepted, self.budget, self.turned_away = {}, SOCKETS_MOST, 0  # F1: address -> [Accepted], oldest first
        self.offer = self.confirm = self.batch = None
        self.idem, self.buckets, self.unlocked, self.proofs, self.handshakes = {}, {}, {}, {}, {}
        self.idem_bytes, self.idem_total = {}, 0  # F7: bytes kept under retry keys, per device and in all
        self.servers, self.addresses, self.magic, self.local_name = {}, ([], []), {}, ''
        self.welcomed = False           # a phone was welcomed this epoch: only then are events kept for resume
        self.loop = self.context = self.keeper = None
        self.tasks, self.handlers = set(), set()
        self.calls = ThreadPoolExecutor(max_workers=4, thread_name_prefix='pebble-remote-call')
        self.stops = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pebble-remote-stop')
        self.bonjour = mac.Bonjour()
        self.mac_state, self.mac_sent = None, 0.0
        self.snapshot = {'running': False, 'connected': [], 'confirm': None, 'pairing': None, 'endpoints': [],
                         'listening': {'wifi': [], 'tailscale': []}, 'port': port}

    # -- starting and stopping ----------------------------------------------------------------------------------------

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.loop.set_exception_handler(quiet)
        self.context = self.identity.tls_context()
        self.budget = socket_budget()
        await self.relisten()
        if '127.0.0.1' not in self.servers:
            raise OSError("Pebble Remote couldn't listen on this Mac.")
        self.keeper = self.spawn(self.housekeeping())
        log.info('Remote bridge started (%d listeners)', len(self.servers))

    async def stop(self):
        self.flush()
        if self.welcomed:
            self.publish('remote.pebble', dumps({'running': False, 'version': self.version}), keys.now_ms())
        if self.keeper is not None:
            self.keeper.cancel()  # no new listener while this one stops (a network change would make one)
        for server in self.servers.values():
            server.close()
        self.servers.clear()
        for conn in list(self.conns):
            conn.close(ws.GOING_AWAY, 'Pebble is stopping')
        if self.confirm and not self.confirm['future'].done():
            self.confirm['future'].set_result(False)
        writers = [conn.writer for conn in self.conns if conn.writer]
        if writers:
            await asyncio.wait(writers, timeout=2)
        for conn in list(self.conns):
            conn.ws.abort()
        for record in [r for group in self.accepted.values() for r in group]:
            self.drop(record)  # sockets still in TLS or the upgrade
        if self.handlers:  # each connection's handler ends on this loop, before the loop itself does
            await asyncio.wait(list(self.handlers), timeout=3)
        left = list(self.tasks) + list(self.handlers)
        for task in left:
            task.cancel()
        await asyncio.gather(*left, return_exceptions=True)
        self.bonjour.stop()
        self.calls.shutdown(wait=False, cancel_futures=True)
        self.stops.shutdown(wait=False, cancel_futures=True)
        self.update(running=False)
        log.info('Remote bridge stopped')

    def spawn(self, coroutine):
        task = asyncio.ensure_future(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def update(self, **changes):
        """A new snapshot for the Mac's Settings page and menu-bar mark, and a note to the host that it changed."""
        self.snapshot = {**self.snapshot, **changes}
        try:
            self.host.changed()
        except Exception:
            log.warning('Remote state could not be shown on the Mac', exc_info=False)

    def connected_now(self):
        return sorted({conn.device for conn in self.conns if conn.device and conn.live})

    # -- listeners and the network --------------------------------------------------------------------------------------

    def endpoints(self):
        """What a pairing link and remote.status list: the LAN addresses and this Mac's .local name (while LAN
        listening is on), the tailnet addresses and their MagicDNS names. Loopback only for the dev entry point."""
        lan = [a for a in self.addresses[0] if a in self.servers] if self.lan else []
        tailnet = [a for a in self.addresses[1] if a in self.servers]
        hosts = (['127.0.0.1'] if self.link_loopback else []) + lan
        if lan and self.local_name:
            hosts.append(self.local_name)
        for address in tailnet:
            hosts.append(address)
            if self.magic.get(address):
                hosts.append(self.magic[address])
        return [keys.format_endpoint(host, self.port) for host in dict.fromkeys(hosts)]

    async def relisten(self):
        """Listen on what the network is now (4.12): servers for addresses that went away are closed, with the phone
        connections that came through them; new addresses get servers on the same port; Bonjour follows."""
        lan, tailnet = self.address_plan()
        wanted = ['127.0.0.1'] + (lan if self.lan else []) + tailnet
        for host in [h for h in self.servers if h not in wanted]:
            self.servers.pop(host).close()
            for conn in [c for c in self.conns if c.local == host]:
                conn.close(ws.GOING_AWAY, 'the network changed')
        for host in wanted:
            if host in self.servers:
                continue
            try:
                self.servers[host] = await self._listen(host)
            except OSError as exc:
                if host == '127.0.0.1' and exc.errno == errno.EADDRINUSE and not self.servers:
                    self.port = 0  # the saved port is taken: another one, kept for next time
                    self.servers[host] = await self._listen(host)
                    self.port = self.servers[host].sockets[0].getsockname()[1]
                    self.host.port_changed(self.port)
                else:
                    log.warning('Remote bridge could not listen on one address (%s)', errno.errorcode.get(exc.errno, '?'))
        for address in tailnet:
            if address not in self.magic:
                self.magic[address] = await self.loop.run_in_executor(self.calls, mac.magic_dns, address)
        served = [a for a in lan if a in self.servers] if self.lan else []
        if served and not self.local_name:
            self.local_name = await self.loop.run_in_executor(self.calls, mac.local_host_name)
        self.addresses = (lan, tailnet)
        if served:
            self.bonjour.advertise(self.bid, self.port)
        else:
            self.bonjour.stop()
        self.update(running=True, endpoints=self.endpoints(), port=self.port,
                    listening={'wifi': served, 'tailscale': [a for a in tailnet if a in self.servers]})

    async def _listen(self, host):
        server = Listener(self, host, self.port)
        if not self.port:
            self.port = server.sockets[0].getsockname()[1]
        return server

    def set_lan(self, on):
        self.lan = bool(on)
        self.spawn(self.relisten())

    def keep_awake_changed(self):
        """The person changed keep awake on the Mac: the phones hear of it at once (remote.mac)."""
        self.spawn(self.mac_event(force=True))

    def set_unlock_seconds(self, seconds):
        """A shorter window applies at once to unlocks already given."""
        self.unlock_seconds = int(seconds)
        limit = time.monotonic() + self.unlock_seconds
        for device, (until, until_ms) in list(self.unlocked.items()):
            if until > limit:
                self.unlocked[device] = (limit, keys.now_ms() + self.unlock_seconds * 1000)

    def admit(self, address):
        """C6: at most 10 handshakes a minute from one address; loopback (this Mac) is exempt (v0.3 item 13)."""
        if not address or address in ('127.0.0.1', '::1'):
            return True
        now = time.monotonic()
        times = self.handshakes.setdefault(address, deque())
        while times and now - times[0] > 60:
            times.popleft()
        if len(times) >= HANDSHAKES_PER_MINUTE:
            return False
        times.append(now)
        return True

    async def housekeeping(self):
        network = minute = time.monotonic()
        while True:
            await asyncio.sleep(1)
            try:
                network, minute = await self._housekeeping(network, minute)
            except Exception as exc:  # one failed pass never ends the timeouts that keep the bridge's promises
                log.warning('Remote housekeeping pass failed (%s)', type(exc).__name__)

    async def _housekeeping(self, network, minute):
        """One pass, every second: the 20 s to authenticate and the 45 s of silence (3.1), the pairing link's end,
        the network every 10 s (4.12), and every minute the sweeps and remote.mac. Returns the next (network, minute)."""
        now = time.monotonic()
        for conn in list(self.conns):
            if conn.confirming:
                continue
            if conn.closing is not None:
                if conn.closing_at is not None and now - conn.closing_at > CLOSING_SECONDS:
                    conn.ws.abort()  # F2: a close that cannot finish (a peer that reads nothing) is cut
                continue
            if conn.device is None and now - conn.opened > AUTH_SECONDS:
                conn.close(ws.POLICY, 'authenticate first')
            elif now - conn.ws.last_received > IDLE_SECONDS:
                log.info('Remote conn#%d silent for 45 s; closed', conn.number)
                conn.close(ws.GOING_AWAY, 'silent too long')
        if self.offer and now > self.offer['expires']:
            self.offer = None
            self.update(pairing=None)
        if now - network >= NETWORK_SECONDS:
            network = now
            if self.address_plan() != self.addresses:
                await self.relisten()
            elif self.lan and self.addresses[0] and self.bonjour.process and self.bonjour.process.poll() is not None:
                self.bonjour.advertise(self.bid, self.port)  # dns-sd ended: advertise again
        if now - minute >= MAC_SECONDS:
            minute = now
            self.sweep(now)
            if self.turned_away:
                log.info('Remote bridge closed %d sockets over its caps in the last minute', self.turned_away)
                self.turned_away = 0
            if any(conn.live for conn in self.conns):
                await self.mac_event()
        return network, minute

    def sweep(self, now):
        for key in [k for k, e in self.idem.items() if now - e['at'] > IDEM_SECONDS and e['future'].done()]:
            size = self.idem.pop(key)['size']
            self.idem_total -= size
            left = self.idem_bytes.get(key[0], 0) - size
            if left > 0:
                self.idem_bytes[key[0]] = left
            else:
                self.idem_bytes.pop(key[0], None)
        for key in [k for k, at in self.proofs.items() if now - at > PROOF_MEMORY_SECONDS]:
            del self.proofs[key]
        for address in [a for a, times in self.handshakes.items() if not times or now - times[-1] > 60]:
            del self.handshakes[address]
        for device in [d for d in self.buckets if d not in self.by_device]:
            del self.buckets[device]
        for conn in self.conns:
            conn.used = {k: at for k, at in conn.used.items() if now - at <= PROOF_MEMORY_SECONDS}

    # -- connections ----------------------------------------------------------------------------------------------------

    def take(self, raw, address, host):
        """F1: a socket just accepted, before any TLS. It counts against its address's cap (loopback, this Mac's own
        processes, has its own), the bridge's share of the open files, and the 8 sockets that may wait to become a
        phone. Over a cap the oldest socket that is not yet a phone gives way (of this address, or of the address
        holding the most), so a phone gets in however many idle sockets others hold; with none to give way, the new
        socket is closed at once."""
        live = [r for group in self.accepted.values() for r in group if not r.dropped]
        mine = [r for r in live if r.address == address]
        crowded = len(mine) >= (PER_LOOPBACK if address in LOOPBACK else PER_ADDRESS)
        if crowded or len(live) >= self.budget or sum(1 for r in live if r.early()) >= MAX_UNAUTHENTICATED:
            victim = self.victim(mine if crowded else live)
            self.turned_away += 1
            if victim is None:
                raw.close()
                return
            self.drop(victim)
        record = Accepted(address, host, raw)
        self.accepted.setdefault(address, []).append(record)
        record.task = self.loop.create_task(self.handle(record))
        self.handlers.add(record.task)
        record.task.add_done_callback(lambda task: self.release(record))

    @staticmethod
    def victim(among):
        """The oldest socket that is not yet a phone, of the address holding the most of `among` (a phone's own address
        rarely does); None when every one is a phone or a pairing the person is answering."""
        early = [r for r in among if r.early()]
        if not early:
            return None
        counts = Counter(r.address for r in among)
        return min(early, key=lambda r: (-counts[r.address], r.at))

    def drop(self, record):
        """Close one socket at once, whatever it is doing (F1)."""
        record.dropped = True
        if record.conn is not None:
            record.conn.ws.abort()
        elif record.writer is not None:
            record.writer.transport.abort()
        else:
            record.task.cancel()  # in its TLS handshake (asyncio closes the socket) or not yet started (release does)

    def release(self, record):
        task = record.task
        self.handlers.discard(task)
        if not task.cancelled() and task.exception() is not None:
            quiet(self.loop, {'exception': task.exception()})  # now, not when the task is collected
        if not record.started:
            record.raw.close()  # cancelled before it ran: nothing else owns the socket
        group = self.accepted.get(record.address, [])
        if record in group:
            group.remove(record)
        if not group:
            self.accepted.pop(record.address, None)

    async def handle(self, record):
        """One accepted socket: TLS and the WebSocket upgrade within HANDSHAKE_SECONDS of accept (F1), C6, then the
        phone. The socket is closed before this ends, so what take() counts is what is open."""
        record.started = True
        self.counter += 1
        number = self.counter
        reader = asyncio.StreamReader(limit=ws.HEAD_LIMIT)
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            try:
                record.raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            try:
                async with asyncio.timeout(HANDSHAKE_SECONDS):
                    transport, _ = await self.loop.connect_accepted_socket(
                        lambda: protocol, record.raw, ssl=self.context, ssl_handshake_timeout=HANDSHAKE_SECONDS,
                        ssl_shutdown_timeout=CLOSE_SECONDS)
                    record.writer = asyncio.StreamWriter(transport, protocol, reader, self.loop)
                    if not self.admit(record.address):
                        log.info('Remote conn#%d refused: more than 10 handshakes a minute from one address', number)
                        await ws.answer(record.writer, '429 Too Many Requests', 'Retry-After: 60\r\n')
                        return
                    socket_ = await ws.upgrade(reader, record.writer, EARLY_MESSAGE)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                log.debug('Remote conn#%d ended before its WebSocket opened', number)
                return
            if socket_ is not None:
                await self.serve(record, number, socket_)
        finally:
            await self.closed(record)

    async def closed(self, record):
        """The socket's file is closed before its count ends (F1); a close still going after CLOSE_SECONDS (a peer that
        never answers TLS's close) is cut."""
        writer = record.writer
        if writer is None:
            return
        if not writer.transport.is_closing():
            writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), CLOSE_SECONDS)
        except Exception:  # the time ran out, or the connection ended with an error: either way, cut it
            writer.transport.abort()

    async def serve(self, record, number, socket_):
        conn = record.conn = Conn(number, socket_, record.host)
        busy = len(self.conns) >= MAX_CONNECTIONS
        self.conns.add(conn)
        conn.writer = asyncio.ensure_future(conn.write())
        if busy:
            log.info('Remote conn#%d refused: busy', number)
            conn.bye('busy')
        else:
            conn.send(dumps({'t': 'challenge', 'v': 1, 'bid': self.bid, 'nonce': conn.nonce, 'time': keys.now_ms()}))
        try:
            while True:
                text = await socket_.recv()
                if text is None:
                    break
                self.on_text(conn, text)
        finally:
            self.conns.discard(conn)
            conn.live = False
            if conn.device and self.by_device.get(conn.device) is conn:
                del self.by_device[conn.device]
                # S-05 (claude-w, 10:49): a device's unlock ends with its last connection (a connection a newer one
                # replaced is not the last: by_device names the newer one by then)
                self.unlocked.pop(conn.device, None)
                self.update(connected=self.connected_now())
            if conn.pairing is not None and not conn.pairing.done():
                conn.pairing.cancel()  # X-1: the phone left before the person answered
            if conn.closing is None:
                conn.closing = (socket_.close_code or ws.NORMAL, '')
                conn.wake.set()
            try:
                await asyncio.wait_for(conn.writer, 3)
            except (asyncio.TimeoutError, asyncio.CancelledError, ConnectionError, OSError):
                conn.writer.cancel()
            if not socket_.closed:
                socket_.abort()

    def on_text(self, conn, text):
        if conn.closing is not None:
            return  # F3, F8: a connection told bye, replaced or closing is heard no more
        size = len(text.encode('utf-8'))
        # F8: before welcome, nothing over the request limit is parsed (EARLY_MESSAGE keeps it far smaller anyway)
        message = parse(text) if size <= MAX_REQUEST or conn.device is not None else None
        if size > MAX_REQUEST:
            # v0.3 item 6: a request over 128 KB that parses and has an id gets res too_large; anything else, bye
            ident = message.get('id') if isinstance(message, dict) else None
            if conn.device is not None and policy.valid_id(ident):
                log.info('Remote conn#%d: a request of %d bytes refused (too_large)', conn.number, size)
                self.refuse(conn, ident, 'too_large')
            else:
                log.info('Remote conn#%d: an unreadable frame of %d bytes (bye too_large)', conn.number, size)
                conn.bye('too_large')
            return
        if not isinstance(message, dict):
            log.info('Remote conn#%d sent a frame that is not a JSON object; closed', conn.number)
            conn.close(ws.BAD_DATA, 'not a JSON object')
            return
        kind = message.get('t')
        if conn.device is None:
            if conn.confirming:
                return  # X-1: what the phone sends while the Mac decides is ignored
            if kind == 'pair':
                self.on_pair(conn, message)
            elif kind == 'hello':
                self.on_hello(conn, message)
            else:
                log.info('Remote conn#%d sent a frame before authenticating; closed', conn.number)
                conn.close(ws.POLICY, 'authenticate first')
            return
        if kind == 'req':
            self.spawn(self.on_req(conn, message))

    # -- pairing (3.2, X-1) -----------------------------------------------------------------------------------------

    def open_pairing(self):
        """A new pairing link's code, valid 5 minutes, for one phone, with at most 5 tries; it replaces an older one.
        Never while the Mac asks the person about a phone (F9): the answer must belong to the digits they see."""
        if self.confirm is not None or any(conn.confirming for conn in self.conns):
            raise ValueError('Answer the iPhone waiting to pair first.')
        code = keys.new_code()
        self.offer = {'code': code, 'expires': time.monotonic() + PAIRING_SECONDS, 'tries': 0, 'used': False}
        until = keys.now_ms() + PAIRING_SECONDS * 1000
        self.update(pairing={'expires_at': until})
        log.info('Remote pairing link opened for 5 minutes')
        return code, until

    def close_pairing(self):
        self.offer = None
        self.update(pairing=None)

    def on_pair(self, conn, message):
        if message.get('v') != 1:
            return conn.bye('bad_version')
        code, offer, good = message.get('code'), self.offer, False
        if offer is not None and not offer['used'] and time.monotonic() < offer['expires'] and \
                offer['tries'] < PAIRING_TRIES and isinstance(code, str):
            good = hmac.compare_digest(code.encode('utf-8'), offer['code'].encode('ascii'))
        if not good:
            if offer is not None and not offer['used']:
                offer['tries'] += 1
            log.info('Remote conn#%d pairing refused: a wrong, used or expired code', conn.number)
            return conn.bye('bad_code')
        try:
            device_key, approve_key = message['device_key'], message['approve_key']
            transcript = keys.t_pair(self.bid, conn.nonce, self.pin, code, device_key, approve_key)
            good = device_key != approve_key and \
                keys.verify(keys.phone_key(device_key), transcript, keys.b64u_decode(message['sig'])) and \
                keys.verify(keys.phone_key(approve_key), transcript, keys.b64u_decode(message['approve_sig']))
        except (KeyError, ValueError, TypeError):
            good = False
        if not good:
            offer['tries'] += 1  # one of the code's five tries; the code stays usable (X-10)
            log.info('Remote conn#%d pairing refused: a signature did not verify', conn.number)
            return conn.bye('bad_signature')
        offer['used'] = True  # spent now: a second phone with the same link is refused while the person decides
        device = message.get('device') if isinstance(message.get('device'), dict) else {}
        conn.confirming = True
        conn.pairing = self.spawn(self.confirm_pairing(conn, device, device_key, approve_key,
                                                       keys.sas_code(transcript), offer))

    async def confirm_pairing(self, conn, device, device_key, approve_key, code, offer):
        """X-1: the Mac shows the six digits and the phone's name; the person confirms that the phone shows the same.
        Cancel, two minutes without an answer, or a phone that leaves: `bye bad_code`, nothing stored, code spent."""
        future = self.loop.create_future()
        name = self.host.device_name(device.get('name'))
        self.confirm = {'future': future, 'conn': conn, 'code': code}
        self.update(pairing=None, confirm={'code': code, 'device': name,
                                           'expires_at': keys.now_ms() + CONFIRM_SECONDS * 1000})
        log.info('Remote conn#%d pairing waits for the confirmation on the Mac', conn.number)
        try:
            confirmed = await asyncio.wait_for(asyncio.shield(future), CONFIRM_SECONDS)
        except asyncio.TimeoutError:
            confirmed = False
        finally:
            if self.confirm and self.confirm['future'] is future:
                self.confirm = None
            conn.confirming = False
            conn.ws.last_received = time.monotonic()
            if self.offer is offer:  # F9: only this pairing's own code and question go
                self.offer = None
            self.update(confirm=None, pairing=self.snapshot.get('pairing') if self.offer else None)
        if not confirmed:
            log.info('Remote conn#%d pairing was not confirmed on the Mac', conn.number)
            return conn.bye('bad_code', PAIR_REFUSED)
        if conn.closing is not None or conn not in self.conns:
            return
        record = self.registry.add(name, device, device_key, approve_key)
        log.info('Remote conn#%d paired device %s', conn.number, short(record['device_id']))
        conn.send(dumps({'t': 'paired', 'device_id': record['device_id']}))
        self.authenticate(conn, record, None)
        self.devices_event()
        self.host.paired(record)

    def answer_pairing(self, confirmed, code=None):
        """The person's answer on the Mac. With `code`, it answers only for those six digits (F9)."""
        if self.confirm and not self.confirm['future'].done() and code in (None, self.confirm['code']):
            self.confirm['future'].set_result(bool(confirmed))
            return True
        return False

    # -- the handshake (3.3) ----------------------------------------------------------------------------------------

    def on_hello(self, conn, message):
        if message.get('v') != 1:
            return conn.bye('bad_version')
        device_id = message.get('device_id')
        if isinstance(device_id, str) and self.registry.was_revoked(device_id):
            return conn.bye('revoked')
        record = self.registry.get(device_id)
        if record is None:
            log.info('Remote conn#%d hello from a device that is not paired', conn.number)
            return conn.bye('unknown_device')
        try:
            good = keys.verify(keys.phone_key(record['device_key']), keys.t_hello(self.bid, device_id, conn.nonce,
                                                                                    self.pin),
                               keys.b64u_decode(message.get('sig')))
        except (ValueError, TypeError):
            good = False
        if not good:
            log.info('Remote conn#%d hello with a signature that did not verify', conn.number)
            return conn.bye('bad_signature')
        self.registry.seen(device_id, message.get('app'))
        self.authenticate(conn, record, message.get('resume'))

    def authenticate(self, conn, record, resume):
        """Welcome, the missed events, then live: no await in between, so no live event overtakes a replayed one."""
        device_id = record['device_id']
        older = self.by_device.get(device_id)
        if older is not None and older is not conn:
            older.bye('replaced')
        conn.device, self.by_device[device_id] = device_id, conn
        conn.ws.max_message = HARD_MESSAGE  # F8: a phone's requests may be larger than pair and hello
        self.flush()
        resumed = self.can_resume(resume)
        conn.send(dumps({'t': 'welcome', 'v': 1, 'epoch': self.epoch, 'seq': self.seq, 'resumed': resumed,
                         'bridge': {'version': self.version, 'name': self.name},
                         'pebble': {'running': True, 'version': self.version},
                         'methods': list(policy.METHODS), 'elevated': list(policy.ELEVATED),
                         'unlock_seconds': self.unlock_seconds,
                         'limits': {'max_request': MAX_REQUEST, 'max_frame': MAX_FRAME}}))
        replayed = 0
        if resumed:
            for seq, _, frame, _ in self.buffer:
                if seq > resume['seq']:
                    conn.send(frame if isinstance(frame, bytes) else frame(device_id))
                    replayed += 1
        conn.live = self.welcomed = True
        log.info('Remote conn#%d welcome device %s resumed=%s replayed=%d', conn.number, short(device_id), resumed,
                 replayed)
        self.update(connected=self.connected_now())

    def can_resume(self, resume):
        if not isinstance(resume, dict) or resume.get('epoch') != self.epoch:
            return False
        seq = resume.get('seq')
        if type(seq) is not int or seq < 0 or seq > self.seq:
            return False
        oldest = self.buffer[0][0] if self.buffer else self.seq + 1
        return seq + 1 >= oldest

    def revoked(self, device_id):
        """4.6: instant. The unlock ends, the connection closes with bye revoked, and the phones hear of it."""
        self.unlocked.pop(device_id, None)
        victim = self.by_device.pop(device_id, None)
        if victim is not None:
            victim.bye('revoked')
        self.devices_event()
        self.update(connected=self.connected_now())

    # -- requests (3.4 to 3.9) ----------------------------------------------------------------------------------------

    def refuse(self, conn, request_id, code, error=None):
        conn.send(dumps({'t': 'res', 'id': request_id, 'ok': False, 'error': error or ERRORS[code], 'code': code}))

    def unlocked_now(self, device):
        until = self.unlocked.get(device)
        return until is not None and until[0] > time.monotonic()

    def current(self, conn):
        """F3: the connection still speaks for its device: not closing, and the device's newest. A request it sent
        that is handled after it closed (or was replaced) does nothing, so no unlock outlives its last connection
        (S-05)."""
        return conn.closing is None and conn.device is not None and self.by_device.get(conn.device) is conn

    def check_proof(self, conn, message, method, reused, idem):
        """3.6 and v0.3: ts within 60 s of the Mac's clock (either way), an id unused on this connection, an r not
        accepted from this device in the last 120 s on any connection (C2; keyed by r, so the (r, n-s) twin is caught
        too), and the approve key's signature over T_proof, bound to this connection's nonce and the call's idem. An
        accepted proof unlocks the device (C1), only on its current connection (F3)."""
        proof = message.get('proof')
        if not isinstance(proof, dict) or reused or not self.current(conn):
            return False
        ts = proof.get('ts')
        if type(ts) is not int or abs(keys.now_ms() - ts) > PROOF_WINDOW_MS:
            return False
        record = self.registry.get(conn.device)
        if record is None:
            return False
        try:
            signature = keys.b64u_decode(proof.get('sig'), 128)
            r = keys.signature_r(signature)
            transcript = keys.t_proof(self.bid, conn.device, self.epoch, conn.nonce, message['id'], idem, method,
                                      message['params_json'], ts)
            approve_key = keys.phone_key(record['approve_key'])
        except (ValueError, TypeError, KeyError):
            return False
        if (conn.device, r) in self.proofs:
            log.info('Remote conn#%d a proof accepted before was sent again (C2)', conn.number)
            return False
        if not keys.verify(approve_key, transcript, signature):
            return False
        now = time.monotonic()
        self.proofs[(conn.device, r)] = now
        self.unlocked[conn.device] = (now + self.unlock_seconds, keys.now_ms() + self.unlock_seconds * 1000)
        return True

    async def known_for(self, method, message):
        """For set_workspace and new_chat with a folder named: the projects listed conversations use, read off the loop
        (a database read) once for the call; otherwise nothing is read."""
        if method not in ('set_workspace', 'new_chat'):
            return lambda: frozenset()
        found = await self.loop.run_in_executor(self.calls, self.host.known_workspaces)
        return lambda: found

    async def on_req(self, conn, message):
        if not self.current(conn):  # F3: the connection closed or was replaced before this request's turn
            return
        started = time.perf_counter()
        request_id = message.get('id')
        if not policy.valid_id(request_id):  # v0.3 item 2
            return self.refuse(conn, request_id if isinstance(request_id, str) and len(request_id) <= 64 and
                               '\n' not in request_id else None, 'bad_request', ID_REFUSED)
        method = message.get('method')
        if not isinstance(method, str):
            return self.refuse(conn, request_id, 'bad_request')
        if not self.buckets.setdefault(conn.device, Bucket()).take():
            log.info('Remote conn#%d %s refused: rate_limited', conn.number, label(method))
            return self.refuse(conn, request_id, 'rate_limited')
        reused = request_id in conn.used
        conn.used[request_id] = time.monotonic()
        if method not in policy.METHOD_SET:
            log.info('Remote conn#%d a method outside the allowlist refused', conn.number)
            return self.refuse(conn, request_id, 'not_allowed')
        idem = message.get('idem')
        if idem is not None and not policy.valid_id(idem):
            return self.refuse(conn, request_id, 'bad_request', RETRY_KEY_REFUSED)
        signed = 'params_json' in message or 'proof' in message
        if idem is None and (signed or method not in policy.READ_ONLY):  # v0.3 item 4 (and idem is a T_proof line)
            return self.refuse(conn, request_id, 'bad_request', RETRY_KEY_REFUSED)
        known = await self.known_for(method, message)
        proven = False
        if 'params_json' in message:
            text = message['params_json']
            if not isinstance(text, str):
                return self.refuse(conn, request_id, 'bad_request')
            if 'proof' not in message:
                log.info('Remote conn#%d %s refused: proof_required', conn.number, method)
                return self.refuse(conn, request_id, 'proof_required')
            if not self.check_proof(conn, message, method, reused, idem):
                log.info('Remote conn#%d %s refused: bad_proof', conn.number, method)
                return self.refuse(conn, request_id, 'bad_proof')
            params = parse(text)  # only once the proof holds (3.6)
            if not isinstance(params, dict):
                return self.refuse(conn, request_id, 'bad_request')
            proven = True
        else:
            if 'proof' in message:
                return self.refuse(conn, request_id, 'bad_request',
                                   'A Face ID confirmation must come with its request as signed text.')
            params = message.get('params', {})
            params = {} if params is None else params
            if not isinstance(params, dict) or reused:
                return self.refuse(conn, request_id, 'bad_request')
            if policy.needs_proof(method, params, known, conn.device):
                log.info('Remote conn#%d %s refused: proof_required', conn.number, method)
                return self.refuse(conn, request_id, 'proof_required')
        if not proven and method not in policy.UNLOCK_FREE and not self.unlocked_now(conn.device):  # C1
            log.info('Remote conn#%d %s refused: unlock_required', conn.number, method)
            return self.refuse(conn, request_id, 'unlock_required')
        if len(dumps(params).encode('utf-8')) > MAX_PARAMS:
            return self.refuse(conn, request_id, 'too_large')
        if method in ('set_workspace', 'new_chat'):
            # v0.5: a folder no conversation uses is connected only when remote.folders would list it. (Without a
            # proof such a call never gets here: needs_proof answered proof_required for every new value.)
            value = params.get('path' if method == 'set_workspace' else 'workspace')
            if policy.workspace_class(value, known) == 'new' and not await self.loop.run_in_executor(
                    self.calls, self.folders.listable, value):
                return self.refuse(conn, request_id, 'not_allowed', WORKSPACE_REFUSED)
        if not self.current(conn):  # F3: it closed while the folders were read
            return
        entry = None
        if idem is not None and method not in policy.READ_ONLY:  # F7: a read is never kept, so never answered again
            entry = self.idem.get((conn.device, idem))
            if entry is not None:
                if entry['method'] != method:
                    return self.refuse(conn, request_id, 'bad_request', 'That retry key belongs to another request.')
                try:  # F11: Pebble may still be at the first one; wait as long as one call may take
                    answer = await asyncio.wait_for(asyncio.shield(entry['future']), CALL_SECONDS)
                except asyncio.TimeoutError:
                    return self.refuse(conn, request_id, 'timeout')
                conn.send(dumps({'t': 'res', 'id': request_id, **answer}))
                log.debug('Remote conn#%d %s answered again from its retry key', conn.number, method)
                return
            if len(self.idem) >= IDEM_LIMIT:
                self.sweep(time.monotonic())
            entry = self.idem[(conn.device, idem)] = {'method': method, 'future': self.loop.create_future(),
                                                      'at': time.monotonic(), 'size': 0}
        after = late = None
        try:
            answer, after, late = await self.execute(conn, method, params)
        except Exception:  # never leave a retry waiting forever
            log.exception('Remote request failed inside the bridge: %s', method)
            answer = {'ok': False, 'error': DEFECT_TEXT}
        answer, frame = self.framed(request_id, answer, method)
        if entry is not None:
            if late is None:
                self.remember(conn.device, entry, answer, frame)
            else:  # F11: the 30 s ran out while Pebble works on; a retry gets what it did once it is done
                device = conn.device
                late.add_done_callback(lambda job: self.remember(
                    device, entry, *self.framed(request_id, self.outcome(method, job), method)))
        conn.send(frame)
        log.debug('Remote conn#%d %s %s %.1f ms', conn.number, method,
                  'ok' if answer.get('ok') else 'refused ' + str(answer.get('code', 'by Pebble')),
                  (time.perf_counter() - started) * 1000)
        if after is not None:
            after()

    def framed(self, request_id, answer, method):
        """(answer, frame): what the phone can read. Too deep, it is clipped (X-5); unwritable, a defect; over
        max_frame, too_large (3.9). A retry gets the same answer."""
        try:
            frame = dumps({'t': 'res', 'id': request_id, **answer})
            if too_deep(frame, MAX_NESTING - 1):
                answer = dict(answer, result=clip(answer.get('result')))
                frame = dumps({'t': 'res', 'id': request_id, **answer})
        except (ValueError, TypeError, RecursionError):
            log.warning('Remote answer to %s could not be written as JSON', method)
            answer = {'ok': False, 'error': DEFECT_TEXT}
            frame = dumps({'t': 'res', 'id': request_id, **answer})
        frame = frame.encode('utf-8')
        if len(frame) > MAX_FRAME:
            answer = {'ok': False, 'error': ERRORS['too_large'], 'code': 'too_large'}
            frame = dumps({'t': 'res', 'id': request_id, **answer}).encode('utf-8')
        return answer, frame

    def remember(self, device, entry, answer, frame):
        """F7: what a retry with this idem gets. The answer itself while it, the device's kept answers and all of them
        stay within IDEM_ANSWER, IDEM_DEVICE and IDEM_BYTES; past them, only that the call happened: ok, code and
        words, with a null result."""
        if entry['future'].done():
            return
        size, used = len(frame), self.idem_bytes.get(device, 0)
        if size > IDEM_ANSWER or used + size > IDEM_DEVICE or self.idem_total + size > IDEM_BYTES:
            slim = {key: answer[key] for key in ('ok', 'code') if key in answer}
            if isinstance(answer.get('error'), str):
                slim['error'] = answer['error'][:1000]
            if slim.get('ok'):
                slim['result'] = None
            answer, size = slim, len(dumps(slim))
        entry['size'] = size
        self.idem_bytes[device] = used + size
        self.idem_total += size
        entry['future'].set_result(answer)

    def outcome(self, method, job):
        """A finished call's answer: Pebble's result or its refusal (F11: also a call that outlived its 30 s)."""
        if job.cancelled():
            return {'ok': False, 'error': ERRORS['timeout'], 'code': 'timeout'}
        if job.exception() is not None:
            return self.host.refusal(method, job.exception())
        return {'ok': True, 'result': job.result()}

    async def execute(self, conn, method, params):
        """(answer, after, late): the answer, what to do once it is sent, and Pebble's job when it outlived its 30 s
        (F11: the job runs on, and its answer is kept for a retry)."""
        if method.startswith('remote.'):
            answer, after = await self.remote(conn, method, params)
            return answer, after, None
        if method in policy.SURFACE_ROUTES:
            params = dict(params, surface='main')
        pool = self.stops if method in STOPPING else self.calls
        job = self.loop.run_in_executor(pool, self.for_phone, conn.device, self.host.call, conn.device, method,
                                        params)
        try:
            await asyncio.wait_for(asyncio.shield(job), CALL_SECONDS)
        except Exception:
            if not job.done():
                return {'ok': False, 'error': ERRORS['timeout'], 'code': 'timeout'}, None, job
        return self.outcome(method, job), None, None

    def for_phone(self, device, function, *args):
        """F4: on a call thread, a phone's job runs only while the phone is still paired. Removing a phone stops the
        calls it queued at once: none reaches Pebble after the removal."""
        if self.registry.get(device) is None:
            raise Removed(BYE['revoked'])
        return function(*args)

    async def blocking(self, function, *args, device=None):
        """`function(*args)` on a call thread, within 30 s; a phone's (`device`) only while it is paired (F4)."""
        if device is not None:
            function, args = self.for_phone, (device, function) + args
        try:
            return await asyncio.wait_for(self.loop.run_in_executor(self.calls, function, *args), CALL_SECONDS)
        except asyncio.TimeoutError:
            raise TimeoutError from None

    async def remote(self, conn, method, params):
        """3.8's methods, C1's remote.unlock, S-05's remote.lock, C3's remote.screenshot and v0.5's remote.folders."""
        def ok(result, after=None):
            return {'ok': True, 'result': result}, after

        def bad(words=None, code='bad_request'):
            return {'ok': False, 'error': words or ERRORS[code], 'code': code}, None

        try:
            if method == 'remote.ping':
                ts = params.get('ts')
                if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                    return bad()
                return ok({'ts': ts, 'mac_ts': keys.now_ms()})
            if method == 'remote.status':
                state = await self.blocking(self.host.mac_state, device=conn.device)
                return ok({'mac': {'name': self.name, **state}, 'pebble': {'running': True, 'version': self.version},
                           'bridge': {'version': self.version, 'epoch': self.epoch, 'endpoints': self.endpoints()}})
            if method == 'remote.devices':
                return ok(self.registry.listing(current=conn.device))
            if method == 'remote.lock':  # S-05: a read, {} to {}; ends this device's unlock at once
                if self.unlocked.pop(conn.device, None) is not None:
                    log.info('Remote device %s locked itself', short(conn.device))
                return ok({})
            if method == 'remote.unlock':  # reached only with an accepted proof, which has just unlocked the device
                return ok({'unlocked_until': self.unlocked[conn.device][1], 'unlock_seconds': self.unlock_seconds})
            if method == 'remote.revoke_device':
                target = params.get('device_id')
                caller = self.registry.get(conn.device) or {}
                if not isinstance(target, str) or not self.registry.revoke(target):
                    return bad("That iPhone isn't paired with this Mac.")
                log.info('Remote device %s removed from a phone', short(target))
                self.unlocked.pop(target, None)
                self.devices_event()
                victim = self.by_device.pop(target, None)
                self.update(connected=self.connected_now())
                self.host.note(conn.device, method, params, caller.get('name'))
                return ok({}, (lambda: victim.bye('revoked')) if victim is not None else None)
            if method == 'remote.keep_awake':  # R3: on by default; the phone may turn it off (or on again)
                if type(params.get('on')) is not bool:
                    return bad()
                held = await self.blocking(self.host.set_keep_awake, params['on'], device=conn.device)
                await self.mac_event(force=True)
                self.host.note(conn.device, method, params)
                return ok({'keep_awake': held})
            if method == 'remote.folders':  # v0.5: needs the unlock, no proof; reads the disk off the loop
                try:
                    return ok(await self.blocking(self.folders.listing, params.get('path'), device=conn.device))
                except mac.FolderRefused as exc:
                    return {'ok': False, 'error': exc.words, **({'code': exc.code} if exc.code else {})}, None
            if method in ('remote.chat_tail', 'remote.chat_before'):
                limit = params.get('limit', 50)
                if type(limit) is not int or not 1 <= limit <= 500 or not isinstance(params.get('id'), str):
                    return bad()
                try:
                    chat = await self.blocking(self.host.chat, params['id'], device=conn.device)
                except ValueError as exc:
                    return {'ok': False, 'error': str(exc)}, None
                messages = chat.get('messages') or []
                if method == 'remote.chat_tail':
                    return ok(dict(chat, messages=messages[-limit:], has_older=len(messages) > limit))
                index = next((i for i, m in enumerate(messages) if m.get('id') == params.get('before')), None)
                if index is None:
                    return {'ok': False, 'error': 'Message not found in this conversation.'}, None
                return ok({'messages': messages[max(0, index - limit):index], 'has_older': index - limit > 0})
            if method == 'remote.screenshot':
                return await self.screenshot(conn, params)
        except TimeoutError:
            return {'ok': False, 'error': ERRORS['timeout'], 'code': 'timeout'}, None
        except Removed as exc:  # F4: removed while this waited for a call thread
            return {'ok': False, 'error': str(exc)}, None
        return bad(code='not_allowed')

    async def screenshot(self, conn, params):
        """C3: {display: index|null} -> {taken_at, displays: [{index, width, height, format, data}]}. Nothing is kept
        on the Mac, and the Mac shows a notice each time."""
        import base64
        wanted = params.get('display')
        if wanted is not None and (type(wanted) is not int or not 0 <= wanted < mac.MAX_DISPLAYS):
            if type(wanted) is int and wanted >= 0:
                return {'ok': False, 'error': 'This Mac has no display %d.' % wanted}, None
            return {'ok': False, 'error': ERRORS['bad_request'], 'code': 'bad_request'}, None
        try:
            shots = await self.blocking(self.host.screenshot, wanted, device=conn.device)
        except mac.ScreenRefused as exc:
            return {'ok': False, 'error': str(exc)}, None
        except TimeoutError:
            return {'ok': False, 'error': ERRORS['timeout'], 'code': 'timeout'}, None
        self.host.note(conn.device, 'remote.screenshot', params)
        return {'ok': True, 'result': {'taken_at': keys.now_ms(), 'displays': [
            {'index': index, 'width': width, 'height': height, 'format': 'jpeg',
             'data': base64.b64encode(data).decode('ascii')} for index, width, height, data in shots]}}, None

    # -- events (3.5, C4) ---------------------------------------------------------------------------------------------

    def tee(self, name, data):
        """Pebble's emit, from any thread. Until a phone has been welcomed in this epoch nothing is kept (no phone could
        resume from it). The data is copied or serialized here, before Pebble's thread moves on."""
        if not self.welcomed or not policy.forwarded(name):
            return
        try:
            if name in ('token', 'reasoning') and isinstance(data, dict):
                payload = dict(data)
            else:
                ids = {k: data[k] for k in ('chat_id', 'run_id', 'id') if isinstance(data, dict)
                       and isinstance(data.get(k), str)}
                payload = (dumps(data), ids)
            self.loop.call_soon_threadsafe(self.pebble_event, name, payload, keys.now_ms())
        except Exception:
            pass  # never into Pebble's emit: an event that cannot be sent is not sent

    def pebble_event(self, name, payload, ts):
        """3.5: consecutive token events of one reply (reasoning of one attempt) within 30 ms of the first held one
        become one; anything else first sends what is held, so nothing is reordered."""
        key = None
        if name == 'token' and isinstance(payload, dict):
            key = ('token', payload.get('chat_id'), payload.get('id'))
        elif name == 'reasoning' and isinstance(payload, dict):
            key = ('reasoning', payload.get('attempt_id'))
        now = time.monotonic()
        held = self.batch
        if held is not None:
            field = 'text' if held['name'] == 'token' else 'delta'
            if key is not None and key == held['key'] and now - held['at'] <= BATCH_SECONDS and \
                    isinstance(held['data'].get(field, ''), str) and isinstance(payload.get(field, ''), str):
                held['data'][field] = held['data'].get(field, '') + payload.get(field, '')
                return
            self.flush()
        if key is not None:
            self.batch = {'key': key, 'name': name, 'data': payload, 'at': now, 'ts': ts,
                          'timer': self.loop.call_later(BATCH_SECONDS, self.flush)}
            return
        text, ids = payload
        self.publish(name, text, ts, ids)

    def flush(self):
        held, self.batch = self.batch, None
        if held is not None:
            held['timer'].cancel()
            data = held['data']
            ids = {k: data[k] for k in ('chat_id', 'run_id', 'id') if isinstance(data.get(k), str)}
            try:
                self.publish(held['name'], dumps(data), held['ts'], ids)
            except ValueError:
                pass

    def publish(self, name, data_text, ts, ids=None):
        """Number the event, keep it for resume and queue it for every live phone. An event over 1 MB, or nested too
        deeply for the phone (X-5), goes as its ids and `trimmed`."""
        self.seq += 1
        frame = ('{"t":"evt","seq":%d,"event":%s,"data":%s,"ts":%d}' % (self.seq, dumps(name), data_text, ts)).encode()
        if len(frame) > MAX_EVENT or too_deep(data_text, MAX_NESTING - 2):
            frame = dumps({'t': 'evt', 'seq': self.seq, 'event': name, 'data': {**(ids or {}), 'trimmed': True},
                           'ts': ts}).encode()
            log.info('Remote event trimmed for the phone (%s)', re.sub(r'[^a-z_.]', '?', name[:40]))
        self.keep(frame, len(frame))

    def keep(self, frame, size):
        now = time.monotonic()
        self.buffer.append((self.seq, now, frame, size))
        self.buffer_bytes += size
        while self.buffer and (len(self.buffer) > REPLAY_HARD or self.buffer_bytes > REPLAY_BYTES or
                               (len(self.buffer) > REPLAY_EVENTS and now - self.buffer[0][1] > REPLAY_SECONDS)):
            self.buffer_bytes -= self.buffer.popleft()[3]
        for conn in list(self.conns):
            if conn.live:
                conn.send(frame if isinstance(frame, bytes) else frame(conn.device))

    def devices_event(self):
        """remote.devices carries the remote.devices method's array (v0.3 item 7): each phone's copy marks it current."""
        self.flush()
        self.seq += 1
        seq, ts, rows = self.seq, keys.now_ms(), self.registry.listing()

        def frame(device_id):
            return dumps({'t': 'evt', 'seq': seq, 'event': 'remote.devices', 'ts': ts,
                          'data': [dict(row, current=row['device_id'] == device_id) for row in rows]}).encode()
        self.keep(frame, 256 * (len(rows) + 1))

    async def mac_event(self, force=False):
        """remote.mac: on a change, at most once a minute (or at once when keep awake changes)."""
        try:
            state = await self.blocking(self.host.mac_state)
        except TimeoutError:
            return
        if force or (state != self.mac_state and time.monotonic() - self.mac_sent >= MAC_SECONDS):
            self.mac_state, self.mac_sent = state, time.monotonic()
            self.flush()
            self.publish('remote.mac', dumps(state), keys.now_ms())
