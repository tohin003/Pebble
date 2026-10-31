"""Phase 46 (1.9.29, D214, D215): what the iPhone bridge asks of the Mac itself.

- Addresses (C6, R1): the private IPv4 addresses of broadcast-capable interfaces (Wi-Fi and Ethernet, the "local
  network" of Apple's TN3179; never a VPN or a public address), and Tailscale's 100.64.0.0/10 address when its tunnel
  is up. Read with getifaddrs, so a network change costs one system call to notice. The tunnel is a point-to-point
  utun interface (w27's review F6): 100.64.0.0/10 is also carrier-grade NAT's shared space (RFC 6598), which a
  hotspot's or a carrier's Wi-Fi hands out to strangers, so that address on any other interface is never listened on.
- Keep awake (R3, 4.10): /usr/bin/caffeinate -i -w <this process>, on battery too; it ends with Pebble's backend.
- Bonjour (3.1, C6): /usr/bin/dns-sd -R as a child process, the instance named by the bridge id, under a small shell
  watchdog that ends it within two seconds of Pebble's backend ending (dns-sd has no -w of its own).
- The battery (`pmset -g batt`), the Mac's names (`scutil`), C3's screenshot (`screencapture` into a private folder,
  scaled and JPEG-encoded with Pillow, deleted at once), and v0.5's folder listing for "Connect a project".
"""

import ctypes
import ctypes.util
import io
import ipaddress
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile

from . import data_home, shell_policy

PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
TAILNET = ipaddress.ip_network('100.64.0.0/10')
IFF_UP, IFF_BROADCAST, IFF_LOOPBACK, IFF_POINTOPOINT, IFF_RUNNING = 0x1, 0x2, 0x8, 0x10, 0x40
CAFFEINATE, DNS_SD, PMSET, SCUTIL, SCREENCAPTURE = ('/usr/bin/caffeinate', '/usr/bin/dns-sd', '/usr/bin/pmset',
                                                   '/usr/sbin/scutil', '/usr/sbin/screencapture')
SERVICE = '_pebble-remote._tcp'
LONG_SIDE = 2560            # C3: the long side of a screenshot
SCREEN_BUDGET = 5_500_000   # JPEG bytes in one answer: under 8 MB once in base64 (3.9)
MAX_DISPLAYS = 8
FOLDERS_LIMIT = 500
# dns-sd under a watchdog: $1 is Pebble's backend, $2 the bridge id, $3 the port. The loop ends when either process
# does, and the trap ends dns-sd with it, so a backend that stops (or crashes) leaves no stale advertisement.
BONJOUR_WATCH = ('trap \'kill "$child" 2>/dev/null\' EXIT; trap \'exit 0\' TERM INT; '
                 '"' + DNS_SD + '" -R "$2" ' + SERVICE + ' local "$3" v=1 "bid=$2" >/dev/null 2>&1 & child=$!; '
                 'while kill -0 "$1" 2>/dev/null && kill -0 "$child" 2>/dev/null; do sleep 2; done')


class _Sockaddr(ctypes.Structure):
    _fields_ = [('len', ctypes.c_uint8), ('family', ctypes.c_uint8), ('port', ctypes.c_uint16),
                ('addr', ctypes.c_uint8 * 4)]


class _Ifaddrs(ctypes.Structure):
    pass


_Ifaddrs._fields_ = [('next', ctypes.POINTER(_Ifaddrs)), ('name', ctypes.c_char_p), ('flags', ctypes.c_uint),
                     ('addr', ctypes.POINTER(_Sockaddr)), ('netmask', ctypes.c_void_p),
                     ('dstaddr', ctypes.c_void_p), ('data', ctypes.c_void_p)]
_libc = None


def interface_addresses():
    """[(interface, flags, IPv4 address)] for this Mac (getifaddrs); [] when it cannot be read."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
        head = ctypes.POINTER(_Ifaddrs)()
        if _libc.getifaddrs(ctypes.byref(head)) != 0:
            return []
    except (OSError, AttributeError):
        return []
    found = []
    try:
        node = head
        while node:
            item = node.contents
            if item.addr and item.addr.contents.family == socket.AF_INET:
                found.append(((item.name or b'').decode('ascii', 'replace'), item.flags,
                              '.'.join(str(b) for b in item.addr.contents.addr)))
            node = item.next
    finally:
        _libc.freeifaddrs(head)
    return found


def address_plan(found=None):
    """(LAN addresses, tailnet addresses), sorted. LAN: RFC 1918 addresses on up, running, broadcast-capable interfaces
    (C6: never a public address, never a VPN). Tailnet: 100.64.0.0/10 on an up, point-to-point utun interface with no
    broadcast (Tailscale's tunnel, F6); that space anywhere else is neither."""
    lan, tailnet = set(), set()
    for name, flags, text in interface_addresses() if found is None else found:
        address = ipaddress.ip_address(text)
        if not flags & IFF_UP or flags & IFF_LOOPBACK:
            continue
        if address in TAILNET:
            if name.startswith('utun') and flags & IFF_POINTOPOINT and not flags & IFF_BROADCAST:
                tailnet.add(text)
        elif flags & IFF_BROADCAST and flags & IFF_RUNNING and any(address in net for net in PRIVATE):
            lan.add(text)
    order = lambda text: ipaddress.ip_address(text)
    return sorted(lan, key=order), sorted(tailnet, key=order)


def _run(argv, timeout=5):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ''
    return done.stdout.strip() if done.returncode == 0 else ''


def mac_name():
    return _run([SCUTIL, '--get', 'ComputerName'])[:64] or socket.gethostname()[:64] or 'Mac'


def local_host_name():
    """`<LocalHostName>.local`, the name Bonjour gives this Mac on the LAN, or ''."""
    name = _run([SCUTIL, '--get', 'LocalHostName'])
    return name + '.local' if re.fullmatch(r'[A-Za-z0-9-]{1,63}', name) else ''


def magic_dns(address, timeout=2.0):
    """Tailscale's MagicDNS name for this Mac's tailnet address (a reverse lookup the tailnet's resolver answers), or
    '' when there is none. Asked only when a tailnet address exists; the lookup never leaves the tailnet's resolver."""
    import concurrent.futures
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='pebble-remote-dns')
    try:
        name = pool.submit(socket.gethostbyaddr, address).result(timeout)[0].rstrip('.')
    except Exception:
        return ''
    finally:
        pool.shutdown(wait=False)
    return name if re.fullmatch(r'[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.ts\.net', name) else ''


def battery():
    """{battery_percent, charging, on_power} from `pmset -g batt`; a Mac without a battery reads as on power."""
    text = _run([PMSET, '-g', 'batt'])
    on_power = "'Battery Power'" not in text
    match = re.search(r'\t(\d{1,3})%;\s*([a-z ]+);', text)
    if not match:
        return {'battery_percent': None, 'charging': False, 'on_power': on_power}
    return {'battery_percent': min(100, int(match.group(1))),
            'charging': match.group(2).strip() in ('charging', 'finishing charge'), 'on_power': on_power}


def _stop(process, group=False):
    if process is None or process.poll() is not None:
        return
    try:
        if group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL) if group else process.kill()
            process.wait(3)
        except (OSError, subprocess.TimeoutExpired):
            pass


class KeepAwake:
    """R3: a power assertion against idle sleep, on battery too, held by caffeinate for as long as this process lives.
    The display may still sleep, and a MacBook still sleeps when its lid closes (4.10)."""

    def __init__(self):
        self.process = None

    @property
    def on(self):
        return self.process is not None and self.process.poll() is None

    def hold(self, on):
        if on and not self.on:
            try:
                self.process = subprocess.Popen([CAFFEINATE, '-i', '-w', str(os.getpid())], stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.process = None
        elif not on:
            _stop(self.process)
            self.process = None
        return self.on


class Bonjour:
    """3.1 and C6: `_pebble-remote._tcp`, the instance named by the bridge id, TXT v=1 and bid=."""

    def __init__(self):
        self.process, self.key = None, None

    def advertise(self, bid, port):
        if self.process is not None and self.process.poll() is None and self.key == (bid, port):
            return
        self.stop()
        try:
            self.process = subprocess.Popen(['/bin/sh', '-c', BONJOUR_WATCH, 'pebble-bonjour', str(os.getpid()), bid,
                                             str(port)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, start_new_session=True)
            self.key = (bid, port)
        except OSError:
            self.process = self.key = None

    def stop(self):
        _stop(self.process, group=True)
        self.process = self.key = None


class ScreenRefused(Exception):
    """C3: a refusal in plain words, with no code (it is the Mac's answer, not the bridge's)."""


NO_SCREEN_RECORDING = ("Pebble can't see your Mac's screen. On the Mac, allow Pebble in System Settings > Privacy & "
                       "Security > Screen & System Audio Recording, then try again.")


def screen_recording_allowed():
    from .companion_tools import _capture_allowed
    return _capture_allowed()


def _jpeg(path, quality, long_side):
    from PIL import Image
    with Image.open(path) as image:
        scale = min(1.0, long_side / max(image.size))
        image.draft('RGB', (max(1, int(image.size[0] * scale)), max(1, int(image.size[1] * scale))))  # JPEG: decode smaller
        image = image.convert('RGB')
        image.thumbnail((long_side, long_side), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, 'JPEG', quality=quality, optimize=True)
        return image.width, image.height, out.getvalue()


def screenshot(display=None, capture=None, allowed=None):
    """C3: [(index, width, height, JPEG bytes)] for every display, or for display `index` alone. The pictures go to a
    private folder only for as long as it takes to read them; it is removed before this returns, whatever happens.
    Raises ScreenRefused with words for the person."""
    allowed = screen_recording_allowed() if allowed is None else allowed
    if allowed is False:
        raise ScreenRefused(NO_SCREEN_RECORDING)
    folder = tempfile.mkdtemp(prefix='pebble-remote-')
    try:
        names = [os.path.join(folder, 'screen-%d.jpg' % i) for i in range(MAX_DISPLAYS if display is None else 1)]
        argv = [SCREENCAPTURE, '-x', '-t', 'jpg'] + ([] if display is None else ['-D', str(display + 1)]) + names
        (capture or _capture)(argv)
        taken = [(i if display is None else display, path) for i, path in enumerate(names) if os.path.isfile(path)
                 and os.path.getsize(path) > 0]
        if not taken:
            if display is not None:
                raise ScreenRefused('This Mac has no display %d.' % display)
            raise ScreenRefused("Pebble couldn't take a screenshot of your Mac. " + NO_SCREEN_RECORDING)
        for quality, side in ((80, LONG_SIDE), (62, LONG_SIDE), (60, 1920)):
            shots = [(index,) + _jpeg(path, quality, side) for index, path in taken]
            if sum(len(shot[3]) for shot in shots) <= SCREEN_BUDGET:
                return shots
        return shots[:1]
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _capture(argv):
    subprocess.run(argv, check=False, timeout=15, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


class FolderRefused(Exception):
    """A refusal of remote.folders (or of a new folder): words, and the bridge's code when there is one."""

    def __init__(self, words, code=None):
        super().__init__(words)
        self.words, self.code = words, code


FOLDER_WORDS = {
    'bad': "Pebble couldn't read that request. Update Pebble Remote if this keeps happening.",
    'outside': 'Pebble Remote shows folders inside your home folder only.',
    'private': 'Pebble keeps hidden and private folders off your iPhone.',
    'missing': 'Choose a folder that exists.',
    'unreadable': "Pebble can't open that folder on your Mac.",
}


class Folders:
    """v0.5 `remote.folders`: the home folder as the phone may browse it for "Connect a project".

    - Subfolders only: never a file, a file name or contents, and never a link.
    - Left out: hidden folders (a leading dot, or macOS's hidden flag, as ~/Library has), Pebble's protected list (the
      Shell sandbox's PROTECTED_HOME, PROTECTED_PREFIX and PROTECTED_NAME, D92) and Pebble's own data folders.
    - A `path` must be the real path of a folder in home, every folder on the way one a listing shows: a link, a `..`,
      a doubled slash or a place outside home is not_allowed, and so is a hidden or protected folder whether or not it
      exists (so the answer tells nothing about what is there).
    - Sorted by name, at most 500; `is_project` when the folder holds `.git` (a folder, or a worktree's file).

    `listable(path)` is the rule set_workspace and new_chat apply to a folder no conversation uses yet."""

    def __init__(self, home=None):
        self.home = os.path.realpath(home or os.path.expanduser('~'))
        self.bases = None

    def _resolve_bases(self):
        home = self.home
        self.bases = ([os.path.realpath(os.path.join(home, entry)).casefold() for entry in shell_policy.PROTECTED_HOME]
                      + [os.path.realpath(root).casefold() for root in data_home.protected_roots()])

    def left_out(self, path, name, info=None):
        if name.startswith('.') or (info is not None and getattr(info, 'st_flags', 0) & getattr(stat, 'UF_HIDDEN', 0)):
            return True
        folded = path.casefold()
        if any(folded == base or folded.startswith(base.rstrip('/') + '/') for base in self.bases):
            return True
        home = self.home.casefold().rstrip('/') + '/'
        if folded.startswith(home) and folded[len(home):].split('/', 1)[0].startswith(shell_policy.PROTECTED_PREFIX):
            return True
        return bool(shell_policy.PROTECTED_NAME.search(path))

    def locate(self, path):
        """The real path of the folder `path` names (None is home), or FolderRefused."""
        self._resolve_bases()
        if path is None:
            return self.home
        if not isinstance(path, str) or not path or len(path) > 4096 or '\x00' in path or not path.startswith('/'):
            raise FolderRefused(FOLDER_WORDS['bad'], 'bad_request')
        given = path.rstrip('/') or '/'
        real = os.path.realpath(given)
        if real != given or not (real == self.home or real.startswith(self.home.rstrip('/') + '/')):
            raise FolderRefused(FOLDER_WORDS['outside'], 'not_allowed')
        here = self.home
        for part in [p for p in real[len(self.home):].split('/') if p]:
            here = os.path.join(here, part)
            try:
                info = os.lstat(here)
            except OSError:
                info = None
            if self.left_out(here, part, info):
                raise FolderRefused(FOLDER_WORDS['private'], 'not_allowed')
        if not os.path.isdir(real):
            raise FolderRefused(FOLDER_WORDS['missing'])
        return real

    def listing(self, path=None):
        """{home, path, parent, folders: [{name, path, is_project}]}; parent is null at home."""
        real = self.locate(path)
        rows = []
        try:
            with os.scandir(real) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            continue
                        info = entry.stat(follow_symlinks=False)
                        entry.name.encode('utf-8')
                    except (OSError, UnicodeEncodeError):
                        continue
                    full = os.path.join(real, entry.name)
                    if not self.left_out(full, entry.name, info):
                        rows.append({'name': entry.name, 'path': full,
                                     'is_project': os.path.lexists(os.path.join(full, '.git'))})
        except PermissionError:
            raise FolderRefused(FOLDER_WORDS['unreadable']) from None
        except OSError:
            raise FolderRefused(FOLDER_WORDS['missing']) from None
        rows.sort(key=lambda row: (row['name'].casefold(), row['name']))
        return {'home': self.home, 'path': real, 'parent': None if real == self.home else os.path.dirname(real),
                'folders': rows[:FOLDERS_LIMIT]}

    def listable(self, path):
        if not isinstance(path, str):
            return False
        try:
            return self.locate(path) != self.home
        except FolderRefused:
            return False
