"""Phase 46 (1.9.29, D214, D215): Pebble Remote on the Mac: the iPhone bridge's switch, its records and its Settings.

main.py makes one RemoteHub before the App and tees every event to it. While Remote is off (the default, contract
4.1) the hub holds no thread, no socket and no child process, and tee() is one attribute check: nothing a run does
changes. Turning Remote on (Settings > iPhone, the main window only) starts the bridge (remote_bridge.py) on a thread
of its own, with a power assertion against idle sleep (R3); turning it off stops both and ends every phone's
connection.

Kept owner-only in <data>/remote/, as Pebble's runtime key is (4.9), and never logged:
- remote.json: the bridge id, its port, and the person's switches (on, LAN listening, keep awake, unlock minutes);
- devices.json: the paired phones (id, name, both public keys, times) and the ids removed;
- activity.json: the last 100 things phones did ("iPhone 17 approved: npm test"), shown on the Mac (4.8, C7);
- tls-key.pem and tls-cert.pem (remote_keys.Identity).

The Mac's own routes (remote_status, remote_set, remote_pair_start, remote_pair_cancel, remote_pair_confirm,
remote_device_remove) are the main window's (main.rs ALLOWED, never the pet) and never the phone's
(remote_policy.MAC_ROUTES): a pairing is opened and confirmed only on the Mac, by the person (X-1).
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from . import VERSION, data_home
from . import remote_policy as policy

log = logging.getLogger('pebble.remote')

FOLDER, STATE, DEVICES, ACTIVITY = 'remote', 'remote.json', 'devices.json', 'activity.json'
UNLOCK_CHOICES = (5, 15, 30, 60)
DEFAULTS = {'enabled': False, 'lan': True, 'keep_awake': True, 'unlock_minutes': 15, 'port': 47800, 'bid': None}
ACTIVITY_LINES, REVOKED_KEPT = 100, 200
MAC_STATE_SECONDS = 30.0
START_SECONDS, STOP_SECONDS = 15.0, 10.0
DEFECTS = (TypeError, KeyError, AttributeError, IndexError, NameError)
DEFECT_TEXT = "Pebble couldn't do that because of an internal error. It is recorded in Pebble's log."
ERROR_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')
# What a phone approved, in a few words, when the card is not a command or an edit.
CARD_WORDS = {'git': 'a Git step', 'worktree': 'a worktree', 'install': 'an install', 'app': 'opening an app',
              'url': 'opening a page', 'browser_grant': 'using websites', 'screen_grant': 'using a window',
              'screen_raise': 'bringing a window forward', 'app_action': 'an app action',
              'plugin': 'a connector action', 'screen': 'a screen action'}
OFF = 'Turn on Pebble Remote first.'


def plain(text, limit):
    from .notifications import plain as one_line
    return one_line(text, limit)


def _settings(stored):
    values = dict(DEFAULTS)
    if isinstance(stored, dict):
        for key in ('enabled', 'lan', 'keep_awake'):
            if type(stored.get(key)) is bool:
                values[key] = stored[key]
        if stored.get('unlock_minutes') in UNLOCK_CHOICES:
            values['unlock_minutes'] = stored['unlock_minutes']
        if type(stored.get('port')) is int and 1024 <= stored['port'] <= 65535:
            values['port'] = stored['port']
        if isinstance(stored.get('bid'), str) and re.fullmatch(r'b-[A-Za-z0-9_-]{16}', stored['bid']):
            values['bid'] = stored['bid']
    return values


class Registry:
    """devices.json: the paired phones and the ids removed. Shared by the bridge's thread and Pebble's call threads."""

    def __init__(self, path):
        self.path, self.lock = Path(path), threading.Lock()
        data = data_home.read_json(self.path) or {}
        devices = data.get('devices') if isinstance(data.get('devices'), dict) else {}
        self.devices = {key: value for key, value in devices.items() if isinstance(value, dict) and all(
            isinstance(value.get(field), str) for field in ('device_id', 'name', 'device_key', 'approve_key'))}
        revoked = data.get('revoked') if isinstance(data.get('revoked'), dict) else {}
        self.revoked = {key: value for key, value in revoked.items() if type(value) is int}

    def _save(self):
        data_home.private_dir(self.path.parent)
        data_home.write_json(self.path, {'format': 'pebble-remote-devices/1', 'devices': self.devices,
                                         'revoked': self.revoked})

    def add(self, name, device, device_key, approve_key):
        from .remote_keys import b64u, now_ms
        with self.lock:
            device_id, now = 'd-' + b64u(os.urandom(12)), now_ms()
            self.devices[device_id] = {
                'device_id': device_id, 'name': name, 'model': plain(device.get('model'), 40),
                'os': plain(device.get('os'), 40), 'app': plain(device.get('app'), 40), 'device_key': device_key,
                'approve_key': approve_key, 'paired_at': now, 'last_seen': now}
            self._save()
            return dict(self.devices[device_id])

    def get(self, device_id):
        with self.lock:
            record = self.devices.get(device_id) if isinstance(device_id, str) else None
            return dict(record) if record else None

    def was_revoked(self, device_id):
        return device_id in self.revoked

    def revoke(self, device_id):
        from .remote_keys import now_ms
        with self.lock:
            if not isinstance(device_id, str) or device_id not in self.devices:
                return False
            del self.devices[device_id]
            self.revoked[device_id] = now_ms()
            for old in sorted(self.revoked, key=self.revoked.get)[:-REVOKED_KEPT]:
                del self.revoked[old]
            self._save()
            return True

    def seen(self, device_id, app=None):
        from .remote_keys import now_ms
        with self.lock:
            record = self.devices.get(device_id)
            if record is not None:
                record['last_seen'] = now_ms()
                if isinstance(app, str):
                    record['app'] = plain(app, 40)
                self._save()

    def listing(self, current=None):
        """remote.devices (3.8): [{device_id, name, paired_at, last_seen}] (+ current for the asking phone)."""
        with self.lock:
            rows = [{'device_id': d['device_id'], 'name': d['name'], 'paired_at': d['paired_at'],
                     'last_seen': d['last_seen']} for d in self.devices.values()]
        if current is not None:
            for row in rows:
                row['current'] = row['device_id'] == current
        return rows


class RemoteHub:
    """The switch, the records, the Mac's routes, and Pebble's side of the bridge (its `host`)."""

    def __init__(self):
        self.bridge = None  # tee() reads only this while Remote is off
        self.loop = self.thread = self.app = self.home = self.folder = self._emit = None
        self.registry = self.keep_awake = self.error = None
        self.settings = dict(DEFAULTS)
        self.activity = deque(maxlen=ACTIVITY_LINES)
        self.lock, self.activity_lock = threading.RLock(), threading.Lock()
        self.dev = {}
        self._mac = (0.0, None)

    # -- main.py's side -------------------------------------------------------------------------------------------------

    def tee(self, name, data):
        """Every event Pebble emits. While Remote is off this is the whole cost: one attribute read."""
        bridge = self.bridge
        if bridge is not None:
            bridge.tee(name, data)

    def attach(self, app, home, emit, **dev):
        """After the App starts: read the person's switches, and start the bridge if they left Remote on. `dev` is the
        dev entry point's (scripts/remote_bridge_dev.py): a test home for folders, loopback in links, a Mac name."""
        self.app, self.home, self._emit, self.dev = app, Path(home), emit, dev
        self.folder = self.home / FOLDER
        if not os.path.isdir(self.folder):
            return
        try:
            self.settings = _settings(data_home.read_json(self.folder / STATE))
            self.registry = Registry(self.folder / DEVICES)
            stored = data_home.read_json(self.folder / ACTIVITY) or {}
            self.activity.extend(line for line in stored.get('lines', []) if isinstance(line, dict)
                                 and isinstance(line.get('text'), str) and type(line.get('at')) is int)
            if self.settings['enabled']:
                self._start()
        except Exception as exc:
            self.error = "Pebble Remote couldn't start. Turn it off and on again in Settings › iPhone."
            log.warning('Remote did not start (%s)', type(exc).__name__)

    def owns(self, method):
        return method in policy.MAC_ROUTES

    def dispatch(self, method, params):
        if self.app is None:
            raise ValueError('Pebble Remote starts once Pebble has finished starting.')
        return getattr(self, method)(**params)

    def close(self):
        self._stop(changed=False)

    # -- starting and stopping ----------------------------------------------------------------------------------------

    def _save(self):
        data_home.private_dir(self.folder)
        data_home.write_json(self.folder / STATE, {'format': 'pebble-remote/1', **self.settings})

    def _start(self):
        from . import remote_bridge, remote_keys, remote_mac
        with self.lock:
            if self.bridge is not None:
                return
            data_home.private_dir(self.folder)
            if not self.settings['bid']:
                self.settings['bid'] = 'b-' + remote_keys.b64u(os.urandom(12))
                self._save()
            identity = remote_keys.Identity(self.folder)
            self.registry = self.registry or Registry(self.folder / DEVICES)
            self.keep_awake = remote_mac.KeepAwake()
            self.keep_awake.hold(self.settings['keep_awake'])
            bridge = remote_bridge.Bridge(
                self, identity, self.registry, bid=self.settings['bid'], name=self.dev.get('name') or
                remote_mac.mac_name(), port=self.dev.get('port', self.settings['port']), lan=self.settings['lan'],
                unlock_seconds=self.settings['unlock_minutes'] * 60,
                folders=remote_mac.Folders(self.dev.get('folders_home')), version=VERSION,
                link_loopback=bool(self.dev.get('link_loopback')), address_plan=self.dev.get('address_plan'))
            loop, ready = asyncio.new_event_loop(), concurrent.futures.Future()

            def run():
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(bridge.start())
                except BaseException as exc:
                    ready.set_exception(exc)
                    return
                ready.set_result(True)
                loop.run_forever()

            thread = threading.Thread(target=run, name='pebble-remote', daemon=True)
            self.loop, self.thread = loop, thread
            thread.start()
            try:
                ready.result(START_SECONDS)
            except BaseException:
                self.loop = self.thread = None
                self._end(loop, thread, bridge)
                self.keep_awake.hold(False)
                self.keep_awake = None
                raise
            self.bridge, self.error = bridge, None
        self.changed()

    def _end(self, loop, thread, bridge):
        if loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(bridge.stop(), loop).result(STOP_SECONDS)
            except Exception as exc:
                log.warning('Remote bridge did not stop cleanly (%s)', type(exc).__name__)
            loop.call_soon_threadsafe(loop.stop)
        thread.join(STOP_SECONDS)
        if not thread.is_alive():
            loop.close()

    def _stop(self, changed=True):
        with self.lock:
            bridge, loop, thread = self.bridge, self.loop, self.thread
            self.bridge = self.loop = self.thread = None  # tee() stops at once
            if bridge is not None:
                self._end(loop, thread, bridge)
            if self.keep_awake is not None:
                self.keep_awake.hold(False)
                self.keep_awake = None
        if changed:
            self.changed()

    def _in_loop(self, function, *args):
        """`function(*args)` on the bridge's loop (where its state lives), from one of Pebble's threads."""
        bridge, loop = self.bridge, self.loop
        if bridge is None:
            raise ValueError(OFF)

        async def call():
            return function(*args)
        return asyncio.run_coroutine_threadsafe(call(), loop).result(STOP_SECONDS)

    # -- the Mac's routes (main window only) --------------------------------------------------------------------------

    def status(self):
        """What Settings > iPhone shows. Read from any thread without the hub's lock (the bridge's thread calls it)."""
        bridge, registry = self.bridge, self.registry
        snapshot = bridge.snapshot if bridge is not None else {}
        connected = set(snapshot.get('connected') or ())
        devices = registry.listing() if registry is not None else []
        return {
            'enabled': self.settings['enabled'], 'running': bool(snapshot.get('running')), 'error': self.error,
            'lan': self.settings['lan'], 'keep_awake': self.settings['keep_awake'],
            'keep_awake_held': bool(self.keep_awake is not None and self.keep_awake.on),
            'unlock_minutes': self.settings['unlock_minutes'], 'unlock_choices': list(UNLOCK_CHOICES),
            'listening': snapshot.get('listening') or {'wifi': [], 'tailscale': []},
            'pairing': snapshot.get('pairing'), 'confirm': snapshot.get('confirm'),
            'devices': [dict(row, connected=row['device_id'] in connected) for row in devices],
            'connected': len(connected),
            'activity': list(self.activity)[-20:][::-1],
        }

    def remote_status(self):
        return self.status()

    def remote_set(self, key, value):
        """The person's switches. Turning Remote on starts the bridge; off stops it with every connection."""
        with self.lock:
            if key == 'enabled':
                if type(value) is not bool:
                    raise ValueError('Choose on or off.')
                if value:
                    try:
                        self.settings['enabled'] = True
                        self._start()
                    except Exception as exc:
                        self.settings['enabled'] = False
                        log.warning('Remote did not start (%s)', type(exc).__name__)
                        raise ValueError("Pebble Remote couldn't start on this Mac. Try again in a moment.") from None
                    finally:
                        self._save()
                else:
                    self.settings['enabled'] = False
                    self._save()
                    self._stop()
                return self.status()
            if key in ('lan', 'keep_awake'):
                if type(value) is not bool:
                    raise ValueError('Choose on or off.')
                self.settings[key] = value
                self._save()
                if key == 'lan' and self.bridge is not None:
                    self._in_loop(self.bridge.set_lan, value)
                if key == 'keep_awake':
                    self.set_keep_awake(value)
                    if self.bridge is not None:
                        self._in_loop(self.bridge.keep_awake_changed)
            elif key == 'unlock_minutes':
                if value not in UNLOCK_CHOICES or type(value) is not int:
                    raise ValueError('Choose 5, 15, 30 or 60 minutes.')
                self.settings['unlock_minutes'] = value
                self._save()
                if self.bridge is not None:
                    self._in_loop(self.bridge.set_unlock_seconds, value * 60)
            else:
                raise ValueError('Setting is not supported.')
        self.changed()
        return self.status()

    def remote_pair_start(self):
        """A new pairing link (5 minutes, one phone), its QR code, and when it ends. Only the main window gets the link:
        it is never emitted, never logged and never stored. Refused while the Mac asks about a phone (F9)."""
        from . import remote_keys, remote_qr
        bridge = self.bridge
        if bridge is None:
            raise ValueError(OFF)
        endpoints = bridge.endpoints()
        if not endpoints:
            raise ValueError('Connect this Mac to Wi-Fi or to Tailscale, then show a pairing code again.')
        code, until = self._in_loop(bridge.open_pairing)
        link = remote_keys.pairing_link(self.settings['bid'], bridge.name, bridge.pin, code, endpoints)
        return {'link': link, 'qr': remote_qr.encode(link), 'expires_at': until, 'status': self.status()}

    def remote_pair_cancel(self):
        if self.bridge is not None:
            self._in_loop(self.bridge.close_pairing)
        return self.status()

    def remote_pair_confirm(self, confirm, code=None):
        """X-1: the person's answer to "Does your iPhone show these six digits?" on the Mac. `code`, the digits the page
        showed, ties the answer to them (w27's review F9): it answers no other phone's pairing."""
        if type(confirm) is not bool:
            raise ValueError('Choose Pair or Cancel.')
        if code is not None and not (isinstance(code, str) and re.fullmatch(r'[0-9]{6}', code)):
            raise ValueError('Choose Pair or Cancel.')
        if self.bridge is None or not self._in_loop(self.bridge.answer_pairing, confirm, code):
            raise ValueError('No iPhone is waiting to pair. Show a new pairing code and try again.')
        return self.status()

    def remote_device_remove(self, device_id):
        """4.6: instant; the phone's connection closes and its keys are refused from now on."""
        registry = self.registry
        if registry is None or not registry.revoke(device_id):
            raise ValueError('That iPhone is not paired with this Mac.')
        log.info('Remote device %s removed on the Mac', str(device_id)[:6])
        if self.bridge is not None:
            self._in_loop(self.bridge.revoked, device_id)
        self.changed()
        return self.status()

    # -- the bridge's host --------------------------------------------------------------------------------------------

    def changed(self):
        """The Settings page and the shell's menu-bar mark follow `remote_state` (a Mac event, never a phone's)."""
        if self._emit is not None:
            try:
                self._emit('remote_state', self.status())
            except Exception:
                pass

    def port_changed(self, port):
        self.settings['port'] = port
        self._save()

    def device_name(self, text):
        return plain(text, 40) or 'iPhone'

    def call(self, device, method, params):
        """A phone's call, on one of the bridge's call threads: Pebble's own dispatch, as the main window's calls reach
        it, then the Mac's record of what the phone did."""
        result = self.app.dispatch(method, params)
        self.note(device, method, params)
        return result

    def refusal(self, method, exc):
        """main.py's answer to a failed call: Pebble's words and its content-free code, or one sentence for a defect.
        A code that is one of the bridge's own is dropped (security review X-6)."""
        from .store import error_kind, friendly_error
        shown = friendly_error(exc) or exc
        answer = {'ok': False, 'error': DEFECT_TEXT if shown is exc and isinstance(exc, DEFECTS) else str(shown)}
        code = getattr(shown, 'code', None)
        if isinstance(code, str) and ERROR_CODE.fullmatch(code) and code not in policy.BRIDGE_CODES:
            answer['code'] = code
        if shown is not exc or (isinstance(exc, ValueError) and not isinstance(exc, (UnicodeError,
                                                                                      json.JSONDecodeError))):
            log.info('Remote request refused: %s (%s)', method, error_kind(exc))
        else:
            log.exception('Remote request failed: %s', method)
        return answer

    def chat(self, chat_id):
        return self.app.chat(chat_id)

    def known_workspaces(self):
        """The projects the listed conversations use (state().chats[].workspace): a phone connects these without a
        proof (v0.5 item 2)."""
        from .store import LISTED_CHATS
        return frozenset(row['workspace'] for row in self.app.store.read(LISTED_CHATS)
                         if isinstance(row['workspace'], str) and row['workspace'])

    def mac_state(self):
        """{battery_percent, charging, on_power, keep_awake} for remote.status and remote.mac; the battery is read
        at most every 30 s."""
        from . import remote_mac
        at, battery = self._mac
        if battery is None or time.monotonic() - at > MAC_STATE_SECONDS:
            battery = remote_mac.battery()
            self._mac = (time.monotonic(), battery)
        return {**battery, 'keep_awake': bool(self.keep_awake is not None and self.keep_awake.on)}

    def set_keep_awake(self, on):
        """R3: the phone or the Mac turns it on or off; the choice is kept until it is changed again."""
        if self.settings['keep_awake'] != on:
            self.settings['keep_awake'] = on
            self._save()
        held = self.keep_awake.hold(on) if self.keep_awake is not None else False
        self.changed()
        return held

    def screenshot(self, display):
        from . import remote_mac
        return remote_mac.screenshot(display, capture=self.dev.get('capture'), allowed=self.dev.get('screen_allowed'))

    def paired(self, record):
        self._record(record['name'] + ' is now paired with this Mac', banner=True)

    def note(self, device, method, params, name=None):
        """4.8 and C7: a line on the Mac for each thing a phone changed; approvals, opening things on the Mac and
        screenshots also post a notification there. `name`: the phone's, when it is no longer paired (it removed
        itself)."""
        words = policy.done_words(method, params)
        if not words:
            return
        if method == 'remote.keep_awake':
            words = 'turned keep awake ' + ('on' if params.get('on') else 'off')
        if method == 'remote.revoke_device':
            words = 'removed itself from this Mac' if params.get('device_id') == device else 'removed another iPhone'
        if method in ('approve', 'approve_simple', 'reject', 'reject_simple'):
            card = self._card(params)
            words += ': ' + card if card else ''
        record = self.registry.get(device) if self.registry is not None else None
        self._record((name or (record or {}).get('name') or 'iPhone') + ' ' + words, banner=method in policy.NOTIFIED)

    def _card(self, params):
        try:
            action = next((a for a in self.app.store.actions(params.get('id')) if a['id'] == params.get('action_id')),
                          None)
        except Exception:
            return ''
        if not action:
            return ''
        payload = action['payload'] if isinstance(action.get('payload'), dict) else {}
        if action.get('kind') in ('command', 'shell') and isinstance(payload.get('command'), str):
            return payload['command']
        if action.get('kind') in ('edit', 'file') and isinstance(payload.get('path'), str):
            return 'an edit to ' + os.path.basename(payload['path'])
        return CARD_WORDS.get(action.get('kind'), 'a review card')

    def _record(self, text, banner=False):
        from .remote_keys import now_ms
        line = {'at': now_ms(), 'text': plain(text, 160)}
        with self.activity_lock:
            self.activity.append(line)
            try:
                data_home.private_dir(self.folder)
                data_home.write_json(self.folder / ACTIVITY, {'format': 'pebble-remote-activity/1',
                                                              'lines': list(self.activity)})
            except OSError as exc:
                log.warning('Remote activity was not saved (%s)', type(exc).__name__)
        if self._emit is not None:
            try:
                self._emit('remote_notice', {'id': uuid.uuid4().hex, **line, 'banner': banner})
            except Exception:
                pass
        self.changed()
