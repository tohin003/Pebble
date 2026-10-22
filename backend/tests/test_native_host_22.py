"""Phase 22 (b22-browser): the native messaging host `com.pebble.browser`.

The host runs in-process with pipes standing in for Chrome's stdio and the real
BridgeServer on a Unix socket under the test's own folder. Nothing here starts
Chrome, writes outside the test folder or reads the person's files.
"""

import ast
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from pebble import browser_bridge as bb
from pebble import native_host as nh


@pytest.fixture
def tmp_path(short_tmp_path):
    """This file's bridges need a socket path under 104 bytes, whatever pytest's basetemp (conftest.short_tmp_path)."""
    return short_tmp_path


HOST_SOURCE = Path(nh.__file__).read_text()


class Chrome:
    """Chrome's side of the stdio pipes: native byte order, one JSON message per frame."""

    def __init__(self):
        to_host_r, self.to_host_w = os.pipe()
        self.from_host_r, from_host_w = os.pipe()
        self.stdin, self.stdout = os.fdopen(to_host_r, 'rb'), os.fdopen(from_host_w, 'wb')
        self.reader = os.fdopen(self.from_host_r, 'rb')
        self.code = None

    def run(self, home, origin=nh.EXTENSION_ORIGIN, peer=nh.peer_uid, extra=()):
        argv = ['--home', str(home), origin, *extra]

        def main():
            try:
                self.code = nh.main(argv, self.stdin, self.stdout, peer=peer)
            finally:
                self.stdout.close()
        self.thread = threading.Thread(target=main, daemon=True)
        self.thread.start()
        return self

    session, send_seq, recv_seq = None, 0, 0

    def send(self, message):
        if self.session is not None:  # paired: every message after the welcome is sealed
            self.send_seq += 1
            message = bb.seal(self.session, 'e2s', self.send_seq, message)
        os.write(self.to_host_w, nh.encode(message))

    def raw(self, data):
        os.write(self.to_host_w, data)

    def read(self):
        frame = nh.read_message(self.reader)
        if frame is None or self.session is None:
            return frame
        self.recv_seq += 1
        return bb.unseal(self.session, 's2e', self.recv_seq, frame)

    def pair(self, welcome):
        """Check the service's proof and switch to sealed messages (what lib/worker.mjs does)."""
        assert welcome['paired'] is True
        assert welcome['proof'] == bb.service_proof(KEY, NONCE, welcome['link_nonce'])
        self.session = bb.session_key(KEY, NONCE, welcome['link_nonce'])

    def close(self):
        try:
            os.close(self.to_host_w)
        except OSError:
            pass

    def wait(self):
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()
        return self.code


KEY, PAIRING_ID, NONCE = bytes(range(32)), 'c' * 32, 'e' * 64
HELLO = {'type': 'hello', 'protocol': nh.PROTOCOL, 'nonce': NONCE, 'pairing_id': PAIRING_ID,
         'extension': {'version': '0.1.0', 'chrome': '130.0'}}


@pytest.fixture
def bridge(tmp_path):
    home = tmp_path / 'h'
    home.mkdir(mode=0o700)
    seen = []
    bridge = bb.BrowserBridge(home, user_home=tmp_path / 'u', pairing=bb.MemoryPairing((PAIRING_ID, KEY, 0.0)))
    bridge.start()
    bridge.home_path = home
    bridge.seen = seen
    yield bridge
    bridge.close()


def until(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, 'timed out waiting'
        time.sleep(0.005)


def test_arguments_are_the_home_and_the_origin_chrome_appends():
    assert nh.parse_args(['--home', '/Users/p/Library/x', 'chrome-extension://abc/']) == ('/Users/p/Library/x', 'chrome-extension://abc/')
    assert nh.parse_args(['--home=/h', 'chrome-extension://abc/', '--parent-window=7']) == ('/h', 'chrome-extension://abc/')
    assert nh.parse_args(['--home', 'relative', 'o']) == (None, 'o')
    assert nh.parse_args(['o']) == (None, 'o')
    assert nh.parse_args(['--home', '/h', 'o', 'p']) == (None, None)
    assert nh.parse_args(['--home', '/h', '--debug', 'o']) == (None, None)


def test_a_foreign_origin_gets_no_answer_at_all(bridge):
    chrome = Chrome().run(bridge.home_path, origin='chrome-extension://aaaabbbbccccddddeeeeffffgggghhhh/')
    assert chrome.wait() == nh.EXIT_ORIGIN
    assert chrome.reader.read() == b''
    assert not bridge.connected


def test_the_relay_carries_both_directions_sealed_and_ends_with_either_side(bridge):
    chrome = Chrome().run(bridge.home_path)
    chrome.send(HELLO)
    welcome = chrome.read()
    assert welcome['type'] == 'welcome' and welcome['protocol'] == nh.PROTOCOL
    chrome.pair(welcome)
    until(lambda: bridge.connected)
    assert bridge.link.version == '0.1.0' and bridge.link.chrome == '130.0'
    assert (bridge.link.ext_nonce, bridge.link.pairing_id) == (NONCE, PAIRING_ID), 'the host passed them through'
    assert 'token' not in json.dumps(welcome), 'the extension never sees the token'
    chrome.send({'type': 'shares', 'shares': [{'tab_id': '4', 'share_id': 'c' * 32, 'origin': 'https://mail.example',
                                               'scope': 'tab', 'state': 'ready', 'nav': 0, 'title': 'Inbox',
                                               'url': 'https://mail.example/'}]})
    until(lambda: bridge.share('4') is not None)
    answers = {}

    def respond():
        call = chrome.read()
        answers['call'] = call
        chrome.send({'type': 'reply', 'id': call['id'], 'ok': True, 'result': {'title': 'Inbox', 'url': 'https://mail.example/'}})
    threading.Thread(target=respond, daemon=True).start()
    assert bridge.request('tab_info', '4') == {'title': 'Inbox', 'url': 'https://mail.example/'}
    assert answers['call'] == {'type': 'call', 'id': answers['call']['id'], 'op': 'tab_info', 'tab_id': '4'}
    chrome.close()  # Chrome closed the port
    assert chrome.wait() == nh.EXIT_OK
    until(lambda: not bridge.connected)


def test_pebble_quitting_ends_the_host(bridge):
    chrome = Chrome().run(bridge.home_path)
    chrome.send(HELLO)
    chrome.pair(chrome.read())
    until(lambda: bridge.connected)
    bridge.close()
    assert chrome.read() == {'type': 'control', 'action': 'bye'}
    assert chrome.read() is None
    assert chrome.wait() == nh.EXIT_OK
    chrome.close()


@pytest.mark.parametrize('bad', ['oversized', 'not_json', 'second_hello', 'no_type'])
def test_a_malformed_message_from_chrome_ends_the_relay(bridge, bad):
    chrome = Chrome().run(bridge.home_path)
    chrome.send(HELLO)
    assert chrome.read()['type'] == 'welcome'
    until(lambda: bridge.connected)
    if bad == 'oversized':
        chrome.raw((nh.MAX_FRAME + 1).to_bytes(4, sys.byteorder))
    elif bad == 'not_json':
        chrome.raw((5).to_bytes(4, sys.byteorder) + b'nope!')
    elif bad == 'second_hello':
        chrome.send({**HELLO, 'token': '0' * 64})
    else:
        chrome.send({'tab_id': '1'})
    assert chrome.wait() == nh.EXIT_PROTOCOL
    until(lambda: not bridge.connected)
    chrome.close()


def test_no_running_pebble_or_a_foreign_bridge_is_reported_to_the_popup(tmp_path, bridge):
    empty = tmp_path / 'empty'
    empty.mkdir(mode=0o700)
    chrome = Chrome().run(empty)
    chrome.send(HELLO)
    assert chrome.read() == {'type': 'status', 'state': 'app_not_running', 'message': nh.NO_APP}
    assert chrome.wait() == nh.EXIT_NO_APP
    chrome = Chrome().run(bridge.home_path, peer=lambda sock: os.getuid() + 1)  # someone else holds the socket
    chrome.send(HELLO)
    assert chrome.read() == {'type': 'status', 'state': 'refused', 'message': nh.REFUSED}
    assert chrome.wait() == nh.EXIT_PROTOCOL
    token = bb.bridge_paths(bridge.home_path).token
    os.chmod(token, 0o644)
    chrome = Chrome().run(bridge.home_path)
    chrome.send(HELLO)
    assert chrome.read()['state'] == 'refused', 'a token others can read is never used'
    os.chmod(token, 0o600)
    fake = tmp_path / 'fake' / bb.BRIDGE_DIR
    fake.mkdir(parents=True, mode=0o700)
    (fake / bb.SOCKET_NAME).write_text('not a socket')
    (fake / bb.TOKEN_NAME).write_text('a' * 64)
    os.chmod(fake / bb.TOKEN_NAME, 0o600)
    chrome = Chrome().run(tmp_path / 'fake')
    chrome.send(HELLO)
    assert chrome.read()['state'] == 'refused'


def test_a_backend_that_refuses_the_hello_is_reported(bridge):
    bb.write_token(bb.bridge_paths(bridge.home_path).token)  # the token on disk no longer matches the server's
    chrome = Chrome().run(bridge.home_path)
    chrome.send(HELLO)
    assert chrome.read() == {'type': 'status', 'state': 'refused', 'message': nh.REFUSED}
    assert chrome.wait() == nh.EXIT_PROTOCOL
    assert not bridge.connected


def test_a_wrong_first_message_ends_the_host_before_it_touches_the_socket(bridge):
    chrome = Chrome().run(bridge.home_path)
    chrome.send({'type': 'shares', 'shares': []})
    assert chrome.wait() == nh.EXIT_PROTOCOL
    assert bridge.server.refused == {'peer': 0, 'path': 0, 'hello': 0, 'busy': 0}
    chrome = Chrome().run(bridge.home_path)
    chrome.close()
    assert chrome.wait() == nh.EXIT_OK, 'Chrome disconnecting at once is a clean end'


@pytest.mark.parametrize('field, value', [('nonce', 'not-hex'), ('nonce', None), ('pairing_id', 'x' * 32)])
def test_the_host_passes_only_hex_pairing_fields_and_a_bad_nonce_is_refused(bridge, field, value):
    chrome = Chrome().run(bridge.home_path)
    chrome.send({**HELLO, field: value})
    reply = chrome.read()
    if field == 'nonce':
        assert reply == {'type': 'status', 'state': 'refused', 'message': nh.REFUSED}
        assert chrome.wait() == nh.EXIT_PROTOCOL
    else:  # a malformed pairing ID is dropped by the host: the backend sees none and welcomes the link unpaired
        assert reply['type'] == 'welcome' and reply['paired'] is False
        chrome.close()
        assert chrome.wait() == nh.EXIT_OK


def test_the_host_uses_only_the_standard_library_and_reads_no_credential():
    tree = ast.parse(HOST_SOURCE)
    imported = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {'json', 'os', 'socket', 'stat', 'struct', 'sys', 'threading'}
    assert not any(isinstance(node, ast.ImportFrom) and node.level for node in ast.walk(tree)), 'no Pebble module'
    lowered = HOST_SOURCE.lower()
    for word in ('keychain', 'vault', 'sqlite', 'pebble.sqlite', 'chatgpt', 'credential_broker', 'subprocess', 'environ'):
        assert word not in lowered.replace('no vault, keychain, database or credential', ''), word
    opened = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, 'attr', '') == 'open']
    assert len(opened) == 1, 'one file is opened: the token'


def test_chrome_frames_use_native_byte_order_and_socket_frames_little_endian():
    data = nh.encode({'type': 'x'})
    assert int.from_bytes(data[:4], sys.byteorder) == len(data) - 4
    assert nh.encode({'type': 'x'}, 'little') == bb.encode_frame({'type': 'x'})
    with pytest.raises(nh.Ended):
        nh.encode({'type': 'x', 'y': 'z' * nh.MAX_FRAME})
