"""Phase 43 (1.9.0, Pebble for Chrome 2): protocol 2, site grants and Pebble's own tabs, over the real bridge socket.

OwnedExtension extends test_browser_bridge_22's FakeExtension (the service worker plus chrome.debugger over a CDP pipe to
FakeChrome) with the owned-tab requests lib/owned.mjs answers: create_tab opens a FakeChrome page and reports it in an
'owned' message, close_tab closes it, list_owned lists them. The screen sessions run on the real ScreenSessions. Nothing
starts a browser, opens a TCP port or reads the person's files.
"""

import json
import re
import socket
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import browser_bridge as bb
from pebble import native_host
from pebble.ax_worker import AXError
from pebble.browser_bridge import BrowserBridge, BrowserRouter, ChromeAdapter, ChromeTabs
from pebble.policy import PolicyError
from pebble.screen_session import ScreenRefused, ScreenSessions
from pebble.store import Store
from test_browser_bridge_22 import KEY, PAIRING_ID, ChromePeer, FakeExtension, row, until
from test_browser_session import ALL, Clock, Page, button, field, text

ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / 'browser-extension'
SHOP, DOCS = 'https://shop.example', 'https://docs.example'


@pytest.fixture
def tmp_path(short_tmp_path):
    return short_tmp_path


def shop_page(url=SHOP + '/item/1'):
    return Page(url, 'Item', [text('A kettle', [10, 10, 200, 20], key='name'),
                              field('Quantity', [10, 40, 80, 20], key='qty', value='1'),
                              button('Add to basket', [10, 70, 120, 24], key='add')])


class OwnedExtension(FakeExtension):
    """The worker's Phase 43 requests, as lib/worker.mjs answers them (its own fence is tested in node)."""

    def __init__(self, home, peer, **kwargs):
        super().__init__(home, peer, **kwargs)
        self.owned, self.next_tab, self.grants_seen = {}, 100, []

    def _report(self):
        self.send({'type': 'owned', 'tabs': [{'tab_id': t, **o} for t, o in self.owned.items()]})

    def _handle(self, message):
        op = message['op']
        if op == 'create_tab':
            self.calls.append((op, None, None))
            url = message['url']
            tab = self.peer.add_tab(shop_page(url) if url.startswith(SHOP) else Page(url, 'Docs', [text('Guide', [10, 10, 100, 20], key='g')]))
            tab_id = str(self.next_tab)
            self.next_tab += 1
            self.add(tab, tab_id)
            origin = url.split('/', 3)
            self.owned[tab_id] = {'grant_id': message['grant_id'], 'origin': '/'.join(origin[:3]), 'state': 'ready', 'nav': 0,
                                  'title': 'Owned', 'url': url, 'acting': False}
            self._report()
            return {'ok': True, 'result': {'tab_id': tab_id}}
        if op == 'close_tab':
            self.calls.append((op, None, message.get('tab_id')))
            if message.get('tab_id') not in self.owned:
                return {'ok': False, 'error': {'code': 'refused', 'message': 'Pebble closes only the tabs it opened.'}}
            self.owned.pop(message['tab_id'])
            self._report()
            return {'ok': True, 'result': {}}
        if op == 'list_owned':
            return {'ok': True, 'result': {'tabs': [{'tab_id': t, **o} for t, o in self.owned.items()]}}
        return super()._handle(message)


def make(tmp_path, *, browser='comet'):
    clock, home = Clock(), tmp_path / 'h'
    store = Store(home)
    holder = {}
    bridge = BrowserBridge(home, on_revoke=lambda tab_id=None: holder['revoke'](tab_id), user_home=tmp_path / 'u',
                           clock=clock.monotonic, wall=clock.wall, pairing=bb.MemoryPairing((PAIRING_ID, KEY, 1_800_000_000.0)))
    tabs = ChromeTabs(bridge, timeout=2.0)
    adapter = ChromeAdapter(tabs, clock=clock.monotonic, sleep=clock.sleep)
    sessions = ScreenSessions(store, {'browser': BrowserRouter({'chrome': adapter})}, clock=clock.monotonic, wall=clock.wall,
                              sleep=clock.sleep)
    holder['revoke'] = lambda tab_id: bb.revoke_chrome_sessions(sessions, tab_id)
    assert bridge.start()['bridge'] == {'listening': True, 'error': None}
    peer = ChromePeer()
    ext = OwnedExtension(home, peer)
    ext.browser = browser
    welcome = connect(ext, browser)
    until(lambda: bridge.connected)
    return SimpleNamespace(clock=clock, home=home, store=store, bridge=bridge, tabs=tabs, adapter=adapter, sessions=sessions,
                           peer=peer, ext=ext, welcome=welcome, chat=uuid.uuid4().hex, run=uuid.uuid4().hex)


def connect(ext, browser):
    """FakeExtension.connect with the extension facts naming the browser (as lib/worker.mjs's hello does)."""
    paths = bb.bridge_paths(ext.home)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(paths.socket))
    sock.sendall(bb.encode_frame({'type': 'hello', 'protocol': bb.PROTOCOL, 'token': bb.read_token(paths.token),
                                  'nonce': ext.ext_nonce, 'pairing_id': ext.pairing_id, 'origin': bb.EXTENSION_ORIGIN,
                                  'host': {'version': '1'},
                                  'extension': {'version': '0.2.0', 'chrome': '155.0.8059.148', 'browser': browser}}))
    sock.settimeout(2.0)
    welcome = bb.read_frame(sock.recv)
    sock.settimeout(None)
    ext.sock, ext.link_nonce = sock, welcome['link_nonce']
    ext.session = bb.session_key(ext.key, ext.ext_nonce, ext.link_nonce)
    threading.Thread(target=ext._loop, daemon=True).start()
    return welcome


@pytest.fixture
def env(tmp_path):
    stack = make(tmp_path)
    yield stack
    stack.ext.close()
    stack.bridge.close()
    stack.store.close()


def site(env, act=(SHOP,), read=(SHOP, DOCS), ttl=600):
    return env.bridge.grant_sites(env.run, list(act), list(read), ttl)


# ---- protocol 2 and the pins shared with the extension -------------------------------------------------------------

def test_protocol_two_is_pinned_on_every_side_and_pairing_keeps_its_label():
    assert bb.PROTOCOL == native_host.PROTOCOL == 'pebble-browser/2'
    assert "export const PROTOCOL = 'pebble-browser/2';" in (EXTENSION / 'lib/policy.mjs').read_text()
    assert bb.PAIRING_LABEL == 'pebble-browser/1'
    assert f"export const PAIRING_LABEL = '{bb.PAIRING_LABEL}';" in (EXTENSION / 'lib/crypto.mjs').read_text()
    ops = re.search(r"export const OPS = Object\.freeze\(\[([^\]]*)\]\)", (EXTENSION / 'lib/policy.mjs').read_text()).group(1)
    assert set(re.findall(r"'([a-z_]+)'", ops)) == bb.OPS, 'the two fences name the same requests'
    functions = re.search(r'READ_ONLY_FUNCTIONS = new Set\(\[ISOLATED_FUNCTIONS\[(\d)\], ISOLATED_FUNCTIONS\[(\d)\]\]\)',
                          (EXTENSION / 'lib/policy.mjs').read_text())
    from pebble import browser_session as bs
    order = [bs.CONTAINS_JS, bs.SELECT_ALL_JS, bs.CHOOSE_JS, bs.OPTION_JS, bs.DETAILS_JS, bs.FOCUSED_JS]
    assert {order[int(functions.group(1))], order[int(functions.group(2))]} == bb.READ_ONLY_FUNCTIONS
    effects = re.search(r"EFFECTS = new Set\(\[([^\]]*)\]\)", (EXTENSION / 'lib/policy.mjs').read_text()).group(1)
    assert set(re.findall(r"'([A-Za-z.]+)'", effects)) == bb.EFFECT_METHODS
    manifest = json.loads((EXTENSION / 'manifest.json').read_text())
    assert manifest['version'] == '0.2.1' and manifest['key'] == bb.manifest_key(), 'a new version, the same identity'
    assert sorted(manifest['permissions']) == ['activeTab', 'debugger', 'nativeMessaging', 'storage', 'tabGroups']


def _hello(home, protocol, version='0.1.0'):
    paths = bb.bridge_paths(home)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(paths.socket))
    sock.settimeout(3.0)
    try:
        sock.sendall(bb.encode_frame({'type': 'hello', 'protocol': protocol, 'token': bb.read_token(paths.token),
                                      'nonce': 'd' * 64, 'pairing_id': None, 'origin': bb.EXTENSION_ORIGIN,
                                      'extension': {'version': version, 'browser': 'chrome'}}))
        return bb.read_frame(sock.recv)
    finally:
        sock.close()


def test_an_older_or_newer_extension_is_told_what_to_reload_or_update_and_the_main_window_sees_it(tmp_path):
    home = tmp_path / 'h'
    home.mkdir(mode=0o700)
    bridge = BrowserBridge(home, user_home=tmp_path / 'u')
    bridge.start()
    try:
        older = _hello(home, 'pebble-browser/1')
        assert older == {'type': 'status', 'state': 'upgrade', 'protocol': bb.PROTOCOL, 'message': bb.UPGRADE_OLDER}
        assert 'Reload' in older['message'] and not bridge.connected
        mismatch = bridge.status()['mismatch']
        assert (mismatch['protocol'], mismatch['expected'], mismatch['version'], mismatch['browser']) == (
            'pebble-browser/1', bb.PROTOCOL, '0.1.0', 'chrome')
        newer = _hello(home, 'pebble-browser/3', '0.3.0')
        assert newer['message'] == bb.UPGRADE_NEWER and 'Update Pebble' in newer['message']
        assert bridge.server.upgrades == 2 and bridge.server.refused['hello'] == 0
        assert _hello(home, 'pebble-browser/2')['type'] == 'welcome'
    finally:
        bridge.close()


def test_the_bridge_learns_the_browser_and_a_live_handshake_follows_the_first_sealed_message(env, tmp_path):
    assert env.bridge.link.browser == 'comet'
    assert env.bridge.status()['browsers']['comet']['handshake'] is None, 'a welcome alone is not a handshake'
    env.ext.share(row())
    until(lambda: env.bridge.share('5') is not None)
    status = env.bridge.status()
    handshake = status['browsers']['comet']['handshake']
    assert handshake['version'] == '0.2.0' and handshake['chrome'] == '155.0.8059.148'
    assert status['extension']['browser'] == 'comet' and status['extension']['protocol'] == bb.PROTOCOL
    assert set(status['browsers']) == set(bb.BROWSER_FOLDERS)
    assert status['host'] == {'chrome': False, 'chrome-beta': False, 'chrome-canary': False, 'chromium': False}, \
        'the Chrome-family map keeps its keys'
    env.ext.send({'type': 'browser', 'browser': 'arc'})
    until(lambda: env.bridge.link.browser == 'arc')
    assert env.bridge.status()['browsers']['arc']['handshake'] is not None
    env.ext.send({'type': 'browser', 'browser': 'safari'})
    until(lambda: env.bridge.link.browser == 'unknown')


# ---- site grants ------------------------------------------------------------------------------------------------------

def test_a_site_grant_holds_exact_sites_and_the_extension_learns_it(env):
    granted = site(env)
    assert granted['act'] == [SHOP] and granted['read'] == [SHOP, DOCS] and granted['expires_in'] == 600
    until(lambda: any(c.get('action') == 'site_grant' for c in env.ext.controls))
    control = next(c for c in env.ext.controls if c.get('action') == 'site_grant')
    assert control == {'type': 'control', 'action': 'site_grant', 'grant_id': granted['grant_id'], 'act': [SHOP],
                       'read': [SHOP, DOCS], 'expires_in': 600}
    assert env.bridge.grant_sites(env.run, [SHOP], [], 60)['read'] == [SHOP], 'every act site is readable'
    for act, read, ttl in (([SHOP + '/x'], [], 60), (['http://shop.example'], [], 60), ([SHOP], [], 0), ([SHOP], [], 1801),
                           ([], [], 60), ([SHOP], [f'https://s{i}.example' for i in range(16)], 60)):
        with pytest.raises(PolicyError):
            env.bridge.grant_sites(env.run, act, read, ttl)
    with pytest.raises(PolicyError):
        env.bridge.grant_sites('not-a-run', [SHOP], [], 60)


def test_a_read_site_joins_only_with_a_reason_and_never_as_an_act_site(env):
    gid = site(env)['grant_id']
    with pytest.raises(PolicyError):
        env.bridge.add_read_origin(gid, 'https://news.example', '')
    added = env.bridge.add_read_origin(gid, 'https://news.example', 'the address came from the task text')
    assert added['added'] is True and 'https://news.example' in added['read']
    grant = env.bridge.site_grant(gid)
    assert grant['act'] == [SHOP] and grant['reads_added'][0]['reason'] == 'the address came from the task text'
    until(lambda: any(c.get('action') == 'site_read' for c in env.ext.controls))
    with pytest.raises(PolicyError):
        env.bridge.add_read_origin('f' * 32, 'https://news.example', 'why')
    env.clock.sleep(601)
    assert env.bridge.site_grant(gid) is None
    with pytest.raises(PolicyError):
        env.bridge.add_read_origin(gid, 'https://more.example', 'why')


# ---- Pebble's own tabs ------------------------------------------------------------------------------------------------

def test_pebble_opens_its_own_tab_only_at_a_read_site_and_never_a_sign_in_or_destructive_address(env):
    gid = site(env)['grant_id']
    for url in ('https://evil.example/', SHOP + '/checkout', DOCS + '/login', SHOP + '/account/delete', 'javascript:x',
                'http://docs.example/'):
        with pytest.raises(PolicyError):
            env.bridge.open_tab(gid, url)
    assert not any(c[0] == 'create_tab' for c in env.ext.calls), 'nothing reached the extension'
    opened = env.bridge.open_tab(gid, DOCS + '/guide')
    assert opened['origin'] == DOCS and re.fullmatch(r'[0-9]+', opened['tab_id'])
    until(lambda: env.bridge.owned(gid) and env.bridge.owned(gid)[0]['title'] == 'Owned')
    assert env.bridge.grant_live(opened['tab_id']) is True
    with pytest.raises(PolicyError):
        env.bridge.open_tab('f' * 32, DOCS + '/guide')


def test_an_owned_tab_reads_on_a_read_site_and_acts_only_on_an_act_site(env):
    gid = site(env)['grant_id']
    tab_id = env.bridge.open_tab(gid, DOCS + '/guide')['tab_id']
    until(lambda: env.bridge.access(tab_id) is not None)
    session = f'tab:{tab_id}'
    env.bridge.request('attach', tab_id)
    tree = env.tabs.conn.call('Page.getFrameTree', session=session)
    assert tree['frameTree']['frame']['id'], 'reading a read site'
    with pytest.raises(bb.CDPError) as exc:
        env.tabs.conn.call('Input.insertText', {'text': 'x'}, session=session)
    assert exc.value.reason == 'read_only'
    with pytest.raises(bb.CDPError) as exc:
        env.tabs.conn.call('Runtime.callFunctionOn', {'functionDeclaration': bb.bs.CHOOSE_JS, 'objectId': 'o',
                                                      'arguments': [{'value': 'x'}], 'returnByValue': True}, session=session)
    assert exc.value.reason == 'read_only', 'choosing an option changes the page'
    shop = env.bridge.open_tab(gid, SHOP + '/item/1')['tab_id']
    until(lambda: env.bridge.access(shop) is not None)
    env.bridge.request('attach', shop)
    env.tabs.conn.call('Page.getFrameTree', session=f'tab:{shop}')
    env.tabs.conn._check('Input.insertText', {'text': 'x'}, shop)  # an act site passes the first fence


def test_a_screen_session_works_on_an_owned_tab_at_an_act_site(env):
    gid = site(env)['grant_id']
    tab_id = env.bridge.open_tab(gid, SHOP + '/item/1')['tab_id']
    until(lambda: env.bridge.access(tab_id) is not None)
    target = {'kind': 'browser', 'profile': 'chrome', 'tab_id': tab_id, 'origins': [SHOP]}
    sid = env.sessions.grant(env.chat, env.run, target, ALL, 600)['session_id']
    view = env.sessions.observe(sid, env.run)
    labels = [e.get('label') for e in view['elements']]
    assert 'Add to basket' in labels and view['window']['origin'] == SHOP
    with pytest.raises(ScreenRefused) as exc:
        env.sessions.grant(env.chat, env.run, {**target, 'origins': [DOCS]}, ALL, 600)
    assert exc.value.code == 'invalid', 'a session on an owned tab takes only the grant\'s act sites'


def test_moving_and_closing_owned_tabs_stay_inside_the_grant(env):
    gid = site(env)['grant_id']
    tab_id = env.bridge.open_tab(gid, DOCS + '/guide')['tab_id']
    until(lambda: env.bridge.access(tab_id) is not None)
    with pytest.raises(PolicyError):
        env.bridge.navigate_owned(tab_id, 'https://evil.example/')
    assert env.bridge.navigate_owned(tab_id, SHOP + '/item/2')['origin'] == SHOP
    assert env.ext.navigated[-1] == SHOP + '/item/2'
    with pytest.raises(PolicyError):
        env.bridge.navigate_owned('5', SHOP + '/item/2')  # not Pebble's tab
    with pytest.raises(PolicyError):
        env.bridge.close_owned('5')
    assert env.bridge.close_owned(tab_id)['closed'] is True
    until(lambda: env.bridge.access(tab_id) is None)


def test_a_lost_answer_to_a_move_is_uncertain_and_never_retried(env):
    gid = site(env)['grant_id']
    tab_id = env.bridge.open_tab(gid, DOCS + '/guide')['tab_id']
    until(lambda: env.bridge.access(tab_id) is not None)
    env.ext.fail['navigate'] = 'timeout'
    with pytest.raises(AXError) as exc:
        env.bridge.navigate_owned(tab_id, SHOP + '/item/3')
    assert exc.value.code == 'uncertain'
    assert sum(1 for c in env.ext.calls if c[0] == 'navigate') == 1


def test_the_end_of_a_grant_the_link_or_stop_ends_its_tabs_sessions(env):
    for how in ('end', 'stop', 'expiry'):
        gid = site(env)['grant_id']
        tab_id = env.bridge.open_tab(gid, SHOP + '/item/1')['tab_id']
        until(lambda: env.bridge.access(tab_id) is not None)
        target = {'kind': 'browser', 'profile': 'chrome', 'tab_id': tab_id, 'origins': [SHOP]}
        sid = env.sessions.grant(env.chat, env.run, target, ALL, 600)['session_id']
        if how == 'end':
            assert env.bridge.end_site_grant(gid) is True
            until(lambda: any(c.get('action') == 'site_ungrant' for c in env.ext.controls))
        elif how == 'stop':
            env.bridge.stop_all('pebble')
        else:
            env.clock.sleep(601)
        if how != 'expiry':
            until(lambda: env.sessions.sessions(env.chat)[-1]['state'] == 'revoked')
        assert env.bridge.grant_live(tab_id) is False, how
        with pytest.raises(ScreenRefused):
            env.sessions.observe(sid, env.run)
        env.ext.owned.clear()
        env.ext._report()
        until(lambda: env.bridge.access(tab_id) is None)


def test_status_lists_site_grants_and_owned_tabs_without_off_grant_addresses(env):
    gid = site(env)['grant_id']
    tab_id = env.bridge.open_tab(gid, DOCS + '/guide')['tab_id']
    until(lambda: env.bridge.status()['owned_tabs'])
    status = env.bridge.status()
    assert [g['grant_id'] for g in status['site_grants']] == [gid]
    assert status['owned_tabs'][0]['tab_id'] == tab_id and status['owned_tabs'][0]['origin'] == DOCS
    env.ext.owned[tab_id].update(state='away', origin='https://evil.example', url='https://evil.example/x?token=1', title='Evil')
    env.ext._report()
    until(lambda: env.bridge.status()['owned_tabs'][0]['state'] == 'away')
    shown = env.bridge.status()['owned_tabs'][0]
    assert shown['origin'] is None and shown['title'] == '' and 'evil' not in json.dumps(env.bridge.owned())


def patched():
    """Whether the parent applied .runtime/phase43-dev/m2-screen_session.patch (a target may name its read sites)."""
    from pebble import screen_session
    try:
        screen_session.validate_target({'kind': 'browser', 'profile': 'chrome', 'tab_id': '1', 'origins': [SHOP],
                                        'read': [SHOP, DOCS]})
    except ScreenRefused:
        return False
    return True


@pytest.mark.xfail(not patched(), strict=True, reason='the parent applies m2-screen_session.patch (read sites)')
def test_with_the_screen_session_patch_a_read_site_is_observed_and_never_acted_on(env):
    gid = site(env)['grant_id']
    tab_id = env.bridge.open_tab(gid, DOCS + '/guide')['tab_id']
    until(lambda: env.bridge.access(tab_id) is not None)
    target = {'kind': 'browser', 'profile': 'chrome', 'tab_id': tab_id, 'origins': [SHOP], 'read': [SHOP, DOCS]}
    sid = env.sessions.grant(env.chat, env.run, target, ALL, 600)['session_id']
    view = env.sessions.observe(sid, env.run)
    assert view['state'] == 'active' and view['window']['origin'] == DOCS, 'a read site is read, not handed back'
    assert view['elements'], 'the read site was read'
    element = view['elements'][0]['ref']
    with pytest.raises(ScreenRefused) as exc:
        env.sessions.act(sid, env.run, view['observation_id'], element, 'focus')
    assert exc.value.code == 'action_not_granted', 'nothing is done on a site the task may only read'
    with pytest.raises(ScreenRefused):
        env.sessions.grant(env.chat, env.run, {**target, 'read': [SHOP, 'https://evil.example']}, ALL, 600)
