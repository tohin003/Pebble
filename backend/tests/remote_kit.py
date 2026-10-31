"""Phase 46 (1.9.29, D214): helpers for Pebble Remote's tests.

- The iOS side's kit (bridge-mock: remote_wire.py, conformance.py) is another session's and read-only here: it is
  loaded without writing bytecode into its folder. PEBBLE_REMOTE_KIT names another copy.
- FakeApp stands in for Pebble's App where a test is about the bridge, not about Pebble.
- Phone is a phone over TLS 1.3 and WebSocket on loopback, built on the kit's own client code.
"""

import asyncio
import importlib.util
import io
import json
import os
import ssl
import sys
import threading
import time
from pathlib import Path

KIT = Path(os.environ.get('PEBBLE_REMOTE_KIT') or Path.home() / 'Projects' / 'PebbleRemote' / 'bridge-mock')


def load(name):
    """A module of the kit, or None when the kit is not on this Mac. No __pycache__ is written into its folder."""
    if name in sys.modules:
        return sys.modules[name]
    path = KIT / (name + '.py')
    if not path.is_file():
        return None
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = before


wire = load('remote_wire')


class Refusal(ValueError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class FakeStore:
    def __init__(self, app):
        self.app = app

    def read(self, sql):
        return [{'workspace': w} for w in sorted(self.app.workspaces)]

    def actions(self, chat_id):
        return self.app.cards.get(chat_id, [])


class FakeApp:
    """Pebble's App as the bridge reaches it: dispatch, chat(), and the store reads the hub makes."""

    def __init__(self):
        self.calls, self.workspaces, self.cards = [], set(), {}
        self.lock = threading.Lock()
        self.store = FakeStore(self)
        self.gate = None

    def called(self, method):
        with self.lock:
            return [params for name, params in self.calls if name == method]

    def dispatch(self, method, params):
        with self.lock:
            self.calls.append((method, json.loads(json.dumps(params))))
        if self.gate is not None:
            self.gate.wait(10)
        if method == 'state':
            return {'chats': [{'id': 'c-known', 'workspace': w} for w in sorted(self.workspaces)],
                    'settings': {'theme': 'system', 'permissions': {'mode': 'ask'}}}
        if method == 'new_chat':
            return {'id': 'chat-%d' % len(self.calls), 'workspace': params.get('workspace')}
        if method in ('approve', 'approve_simple', 'reject', 'reject_simple'):
            if any(card['id'] == params.get('action_id') for card in self.cards.get(params.get('id'), [])):
                return {'status': 'done'}
            raise Refusal('This action is no longer awaiting approval.')
        if method == 'send' and params.get('text') == 'full disk':
            raise Refusal('The disk is full.', 'disk_full')
        if method == 'send' and params.get('text') == 'bridge code':
            raise Refusal('Pebble says unlock.', 'unlock_required')
        if method == 'send' and params.get('text') == 'defect':
            raise KeyError('secret detail')
        if method == 'run_get' and params.get('deep'):
            deep = node = {}
            for _ in range(100):  # model-written arguments in a card can nest this far
                node['next'] = {}
                node = node['next']
            return {'run_id': 'r1', 'args': deep}
        if method == 'run_get' and params.get('nan'):
            return {'run_id': 'r1', 'score': float('nan')}
        return {'method': method}

    def chat(self, chat_id):
        if chat_id != 'c1':
            raise ValueError('Conversation not found.')
        if getattr(self, 'huge', False):
            return {'id': 'c1', 'title': 'One', 'messages': [{'id': 'm0', 'role': 'user', 'content': 'x' * (9 << 20)}]}
        return {'id': 'c1', 'title': 'One', 'messages': [{'id': 'm%d' % i, 'role': 'user', 'content': str(i)}
                                                         for i in range(10)]}


class FakeAwake:
    def __init__(self):
        self.on = False

    def hold(self, on):
        self.on = bool(on)
        return self.on


class FakeBonjour:
    def __init__(self):
        self.process, self.advertised = None, []

    def advertise(self, bid, port):
        self.advertised.append((bid, port))

    def stop(self):
        pass


class Events:
    """The hub's emit: every Mac event, kept in order."""

    def __init__(self):
        self.items, self.lock = [], threading.Lock()

    def __call__(self, name, data):
        with self.lock:
            self.items.append((name, json.loads(json.dumps(data))))

    def named(self, name):
        with self.lock:
            return [data for event, data in self.items if event == name]

    def wait(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                for name, data in self.items:
                    if predicate(name, data):
                        return data
            time.sleep(0.02)
        raise AssertionError('no matching event')


SCREENS = ((3024, 1964), (1920, 1080))


def fake_capture(argv):
    """screencapture's stand-in: a test pattern for each display it is asked for (never this Mac's screen)."""
    from PIL import Image
    names = [arg for arg in argv[1:] if arg.endswith('.jpg')]
    wanted = int(argv[argv.index('-D') + 1]) - 1 if '-D' in argv else None
    for index, (width, height) in enumerate(SCREENS):
        if wanted is not None and index != wanted:
            continue
        path = names[0] if wanted is not None else names[index]
        image = Image.new('RGB', (width, height), (40 + 60 * index, 120, 90))
        image.save(path, 'JPEG', quality=70)


def start_hub(home, app, monkeypatch, **dev):
    from pebble import remote, remote_mac
    monkeypatch.setattr(remote_mac, 'KeepAwake', FakeAwake)
    monkeypatch.setattr(remote_mac, 'Bonjour', FakeBonjour)
    monkeypatch.setattr(remote_mac, 'local_host_name', lambda: 'Test-Mac.local')
    monkeypatch.setattr(remote_mac, 'magic_dns', lambda address: '')  # never a DNS query from a test
    monkeypatch.setattr(remote_mac, 'battery', lambda: {'battery_percent': 80, 'charging': False, 'on_power': False})
    events = Events()
    hub = remote.RemoteHub()
    tree = Path(home) / 'tree'
    tree.mkdir(exist_ok=True)
    options = dict(name='Test Mac', folders_home=str(tree), link_loopback=True, address_plan=lambda: ([], []), port=0,
                   capture=fake_capture, screen_allowed=True)
    options.update(dev)
    hub.attach(app, Path(home) / 'data', events, **options)
    hub.remote_set('enabled', True)
    return hub, events


class Pacer:
    """Under the bridge's rate limit (20 at once, 10 a second), as conformance.py keeps itself: 12 at once, 8 a second."""

    def __init__(self, burst=12, rate=8.0):
        self.burst, self.rate = burst, rate
        self.tokens, self.at = float(burst), time.monotonic()

    async def take(self):
        while True:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
            self.at = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep((1 - self.tokens) / self.rate)


class Phone:
    """A phone: two P-256 keys (the kit's software stand-ins for the Secure Enclave's), and its connections."""

    def __init__(self, link):
        self.link = wire.parse_pairing_link(link)
        self.port = self.link['endpoints'][0][1]
        self.device_key, self.approve_key = wire.SigningKey(), wire.SigningKey()
        self.device_id = None
        self.count = 0
        self.pacer = Pacer()

    def connection(self):
        return Connection(self)

    def next_id(self):
        self.count += 1
        return 'r%d' % self.count


class Connection:
    def __init__(self, phone):
        self.phone, self.frames, self.closed = phone, asyncio.Queue(), False
        self.seen, self.ws, self.challenge, self.welcome, self.task = [], None, None, None, None

    @property
    def nonce(self):
        return self.challenge['nonce']

    async def open(self, check_pin=True):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        reader, writer = await asyncio.open_connection('127.0.0.1', self.phone.port, ssl=context)
        ssl_object = writer.get_extra_info('ssl_object')
        self.tls_version = ssl_object.version()
        if check_pin:
            assert wire.certificate_pin(ssl_object.getpeercert(binary_form=True)) == self.phone.link['pin']
        self.ws, self.headers = await wire.client_handshake(reader, writer, '127.0.0.1:%d' % self.phone.port)
        self.task = asyncio.ensure_future(self._read())
        self.challenge = await self.next()
        return self

    async def _read(self):
        while True:
            message = await self.ws.recv()
            if message is None:
                self.closed = True
                await self.frames.put(None)
                return
            frame = json.loads(message)
            self.seen.append(frame)
            await self.frames.put(frame)

    async def next(self, timeout=10):
        return await asyncio.wait_for(self.frames.get(), timeout)

    async def until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            frame = await self.next(max(0.01, deadline - time.monotonic()))
            if frame is None:
                return None
            if predicate(frame):
                return frame

    async def event(self, predicate, timeout=10):
        """A frame already seen (waiting for an answer passes over the events that came first), or the next one."""
        for frame in self.seen:
            if frame is not None and predicate(frame):
                return frame
        return await self.until(predicate, timeout)

    async def send(self, frame):
        await self.ws.send_text(frame if isinstance(frame, str) else json.dumps(frame, separators=(',', ':')))

    async def pair(self, code=None, sas=None):
        phone, link = self.phone, self.phone.link
        code = link['code'] if code is None else code
        transcript = wire.t_pair(link['bid'], self.nonce, link['pin'], code, phone.device_key.x963_b64u,
                                 phone.approve_key.x963_b64u)
        if sas is not None:
            sas.append(wire.sas_code(transcript))
        await self.send({'t': 'pair', 'v': 1, 'code': code, 'device': {'name': 'iPhone 17', 'model': 'iPhone18,3',
                                                                         'os': 'iOS 26.1', 'app': '1.0 (1)'},
                         'device_key': phone.device_key.x963_b64u, 'approve_key': phone.approve_key.x963_b64u,
                         'sig': wire.b64u(phone.device_key.sign(transcript)),
                         'approve_sig': wire.b64u(phone.approve_key.sign(transcript))})

    async def hello(self, resume=None, key=None, nonce=None, device_id=None, version=1):
        phone, link = self.phone, self.phone.link
        device_id = device_id or phone.device_id
        transcript = wire.t_hello(link['bid'], device_id, nonce or self.nonce, link['pin'])
        await self.send({'t': 'hello', 'v': version, 'device_id': device_id,
                         'sig': wire.b64u((key or phone.device_key).sign(transcript)), 'resume': resume,
                         'app': '1.0 (1)'})
        frame = await self.until(lambda f: f['t'] in ('welcome', 'bye'))
        if frame and frame['t'] == 'welcome':
            self.welcome = frame
        return frame

    async def call(self, method, params=None, idem=True, request_id=None):
        request_id = request_id or self.phone.next_id()
        frame = {'t': 'req', 'id': request_id, 'method': method, 'params': {} if params is None else params}
        if idem:
            frame['idem'] = idem if isinstance(idem, str) else 'i-' + request_id + '-' + str(time.monotonic_ns())
        return await self.exchange(frame)

    async def exchange(self, frame, timeout=10):
        await self.phone.pacer.take()
        await self.send(frame)
        return await self.until(lambda f: f['t'] == 'res' and f.get('id') == frame['id'], timeout)

    def elevated(self, method, params, *, request_id=None, idem=None, key=None, ts=None, epoch=None, nonce=None,
                 sign_method=None, sign_idem=None, params_json=None, signed_json=None, v02=False):
        phone, link = self.phone, self.phone.link
        request_id = request_id or phone.next_id()
        idem = idem or 'e-' + request_id + '-' + str(time.monotonic_ns())
        text = params_json if params_json is not None else json.dumps(params, separators=(',', ':'))
        signed = signed_json if signed_json is not None else text
        ts = wire.now_ms() if ts is None else ts
        epoch = epoch or self.welcome['epoch']
        if v02:
            transcript = wire.t_proof_v02(link['bid'], phone.device_id, epoch, request_id, sign_method or method,
                                          signed, ts)
        else:
            transcript = wire.t_proof(link['bid'], phone.device_id, epoch, nonce or self.nonce, request_id,
                                      sign_idem or idem, sign_method or method, signed, ts)
        signature = (key or phone.approve_key).sign(transcript)
        return {'t': 'req', 'id': request_id, 'method': method, 'params_json': text, 'idem': idem,
                'proof': {'ts': ts, 'sig': wire.b64u(signature)}}

    async def unlock(self):
        answer = await self.exchange(self.elevated('remote.unlock', {}))
        assert answer['ok'] is True, answer
        return answer

    async def close(self):
        if self.ws is not None and not self.ws.closed:
            try:
                await self.ws.close(wire.CLOSE_NORMAL, 'done')
            except (ConnectionError, OSError):
                pass
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, 3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()


async def paired(hub, events, phone=None):
    """A phone paired through the whole of 3.2 and X-1 (the test confirms on the Mac): its open connection."""
    link = hub.remote_pair_start()['link']
    phone = phone or Phone(link)
    if phone.link['code'] != wire.parse_pairing_link(link)['code']:
        phone.link = wire.parse_pairing_link(link)
    connection = await phone.connection().open()
    sas = []
    await connection.pair(sas=sas)
    await asyncio.to_thread(events.wait, lambda name, data: name == 'remote_state' and (data.get('confirm') or {})
                            .get('code') == sas[0])
    await asyncio.to_thread(hub.remote_pair_confirm, True)
    frame = await connection.until(lambda f: f['t'] in ('paired', 'bye'))
    assert frame['t'] == 'paired', frame
    phone.device_id = frame['device_id']
    connection.welcome = await connection.until(lambda f: f['t'] == 'welcome')
    return phone, connection


def jpeg_size(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        return image.size
