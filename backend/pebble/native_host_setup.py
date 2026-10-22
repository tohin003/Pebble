"""Connect Chrome from Pebble's main window (Phase 24, V1-043; decision D6).

Pebble for Chrome needs Chrome's native-messaging host: exactly the two files HARNESS_BROWSER_EXTENSION.md §5 names,
and nothing else.

launcher  <data home>/chrome/pebble-native-host (0700). It runs Pebble's own backend as the host with a cleared
          environment: `pebble-backend --browser-native-host --home <data home>`.
manifest  ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.pebble.browser.json (0644). Its
          allowed_origins names only the pinned extension.

plan() is read-only: both files with the SHA-256 of the exact bytes Pebble would write, their state (absent, current,
update, foreign, not_a_file), what stands in the way, and a digest of the plan. install() writes only when the caller
passes that digest, which the main window sends after the person's one confirmation, and only while the plan is still
the same. A file Pebble did not write is never replaced or removed (the person is told where it is); nothing else is
written: no backup, no Chrome preference, no extension install, no credential read. scripts/install_native_host.py
builds the same bytes (launcher_text, manifest_bytes) for the parent's staged and real-path runs.

The routes (routes(app); main window only: tool_authz.NATIVE_ONLY and main.rs ALLOWED, never the pet or a model):
browser_connect_status   plan() for Pebble's own data home, or why Chrome cannot connect here (a flat --home).
browser_connect_install  install(confirm=<plan_sha256>).

The extension itself is loaded by the person in Chrome's Developer mode (D6: no Chrome Web Store build for 1.0).
"""

import hashlib
import json
import os
import re
import shlex
import stat
import sys
import time
from pathlib import Path

from .browser_bridge import (BRIDGE_DIR, EXTENSION_ID, EXTENSION_ORIGIN, HOST_FOLDERS, HOST_NAME, HOST_SCRIPT,
                             own_executable)
from .policy import PolicyError

PLAN_FORMAT = 'pebble.connect-chrome/1'
LAUNCHER_MARK = '# pebble.native-host/1'
DESCRIPTION = 'Pebble for Chrome: relays the extension to the Pebble app on this Mac.'
SUPPORT = 'Library/Application Support'
BROWSER = 'chrome'
LAUNCHER_MODE, MANIFEST_MODE, HOSTS_MODE = 0o700, 0o644, 0o755
MAX_FILE = 65_536
HEX64 = re.compile(r'[0-9a-f]{64}')
STATES = ('absent', 'current', 'update', 'foreign', 'not_a_file')
NOT_HERE = "Pebble for Chrome connects only to Pebble's own data folder (~/.pebble)."
NEVER = ('edits Chrome preferences', 'installs or enables the extension', 'reads a credential', 'writes a third file')
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)


class SetupRefused(PolicyError):
    """Nothing was written. code names the check; the message is for the person."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _Unreadable(Exception):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def launcher_text(program, data_home):
    """The launcher. program is the host command: [pebble-backend], or [python, '-E', '-s', backend/main.py]."""
    command = ' '.join(shlex.quote(str(part)) for part in program)
    return ('#!/bin/sh\n'
            f'{LAUNCHER_MARK} (written by Pebble; Chrome runs it for {HOST_NAME})\n'
            "# Chrome passes the calling extension's origin; the host refuses any but Pebble for Chrome.\n"
            f'exec /usr/bin/env -i HOME="$HOME" PATH=/usr/bin:/bin {command} --browser-native-host '
            f'--home {shlex.quote(str(data_home))} "$@"\n')


def manifest_doc(launcher):
    return {'name': HOST_NAME, 'description': DESCRIPTION, 'path': str(launcher), 'type': 'stdio',
            'allowed_origins': [EXTENSION_ORIGIN]}


def manifest_bytes(launcher):
    return (json.dumps(manifest_doc(launcher), indent=2) + '\n').encode('utf-8')


def host_program():
    """The command the host runs: the bundled pebble-backend (the executable the bridge pins), or in a source
    checkout this Python with backend/main.py."""
    if getattr(sys, 'frozen', False):
        return [own_executable()]
    return [sys.executable, '-E', '-s', str(Path(__file__).resolve().parents[1] / 'main.py')]


def paths(data_home, user_home):
    data_home, support = Path(data_home), Path(user_home) / SUPPORT / HOST_FOLDERS[BROWSER]
    return {'data_home': data_home, 'bridge': data_home / BRIDGE_DIR, 'launcher': data_home / BRIDGE_DIR / HOST_SCRIPT,
            'support': support, 'hosts': support / 'NativeMessagingHosts',
            'manifest': support / 'NativeMessagingHosts' / f'{HOST_NAME}.json'}


def _read(path):
    """Bytes of a small regular file that is not a link, None when absent; _Unreadable for anything else."""
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise _Unreadable(str(path)) from None
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise _Unreadable(str(path))
        return handle.read()


def _pebbles(role, data):
    """Whether bytes at a path are a launcher or manifest Pebble wrote (this version or an older one)."""
    if role == 'launcher':
        lines = data.splitlines()
        return len(lines) > 1 and lines[0] == b'#!/bin/sh' and lines[1].startswith(LAUNCHER_MARK.encode())
    try:
        doc = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get('name') == HOST_NAME and doc.get('description') == DESCRIPTION \
        and doc.get('type') == 'stdio' and doc.get('allowed_origins') == [EXTENSION_ORIGIN]


def _state(role, path, wanted):
    try:
        current = _read(path)
    except _Unreadable:
        return 'not_a_file'
    if current is None:
        return 'absent'
    if current == wanted:
        return 'current'
    return 'update' if _pebbles(role, current) else 'foreign'


def _folder_ok(path):
    """True when path is absent or a real folder (never a link)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _wanted(data_home, user_home, program):
    p = paths(data_home, user_home)
    return p, (('launcher', p['launcher'], launcher_text(program, p['data_home']).encode('utf-8'), LAUNCHER_MODE),
               ('manifest', p['manifest'], manifest_bytes(p['launcher']), MANIFEST_MODE))


def plan(data_home, *, user_home, program):
    """Read-only: the two files Connect Chrome writes, what stands in the way, and the digest install() needs."""
    p, wanted = _wanted(data_home, user_home, program)
    files = [{'role': role, 'path': str(path), 'mode': f'{mode:04o}', 'sha256': _sha(data),
              'state': _state(role, path, data)} for role, path, data, mode in wanted]
    problems = []

    def problem(code, message):
        problems.append({'code': code, 'message': message})
    if not (_folder_ok(p['data_home']) and p['data_home'].is_dir()):
        problem('no_data_folder', "Pebble's data folder is missing, so there is nowhere to put the helper.")
    if not _folder_ok(p['bridge']):
        problem('bridge_folder', "Pebble's Chrome folder is a link or not a folder, so nothing was changed.")
    executable = Path(str(program[0])) if program else None
    if executable is None or not executable.is_file() or not os.access(executable, os.X_OK):
        problem('no_backend', "Pebble's own program was not found where the helper would run it.")
    if not (_folder_ok(p['support']) and p['support'].is_dir()):
        problem('chrome_not_found', 'Chrome has not been opened on this Mac yet. Open it once, then try again.')
    if not _folder_ok(p['hosts']):
        problem('hosts_folder', "Chrome's NativeMessagingHosts folder is a link or not a folder, so nothing was "
                                'changed.')
    for item in files:
        if item['state'] in ('foreign', 'not_a_file'):
            problem('not_pebbles', f"A file Pebble did not write is at {item['path']}. Pebble never replaces it: "
                                   'remove it yourself if you no longer need it, then try again.')
    digest = _sha(json.dumps({'format': PLAN_FORMAT, 'files': [[f['role'], f['path'], f['mode'], f['sha256']]
                                                               for f in files]}, sort_keys=True).encode('utf-8'))
    return {'format': PLAN_FORMAT, 'available': True, 'browser': BROWSER, 'extension_id': EXTENSION_ID,
            'files': files, 'installed': all(f['state'] == 'current' for f in files), 'ready': not problems,
            'problems': problems, 'plan_sha256': digest, 'never': list(NEVER), 'authorization': 'none'}


def _write(path, data, mode):
    """A temporary file beside path (O_EXCL), fsync, chmod, rename over path, then read back."""
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{time.time_ns()}')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    try:
        back = _read(path)
    except _Unreadable:
        back = None
    if back != data:
        raise SetupRefused('readback', f'{path} did not read back as written. Try Connect Chrome again.')


def install(data_home, *, user_home, program, confirm):
    """Write the planned files, only with the digest of the plan the person confirmed. Returns the new status."""
    if not isinstance(confirm, str) or not HEX64.fullmatch(confirm):
        raise SetupRefused('confirm', 'Confirm Connect Chrome in Pebble first. Nothing was written.')
    shown = plan(data_home, user_home=user_home, program=program)
    if confirm != shown['plan_sha256']:
        raise SetupRefused('plan_changed', 'What Pebble would write changed since you confirmed it. Check it again; '
                                           'nothing was written.')
    if shown['problems']:
        first = shown['problems'][0]
        raise SetupRefused(first['code'], first['message'] + ' Nothing was written.')
    p, wanted = _wanted(data_home, user_home, program)
    from .browser_bridge import ensure_private_dir
    written = []
    for role, path, data, mode in wanted:
        state = _state(role, path, data)  # again, just before writing: never over a file someone put there meanwhile
        if state == 'current':
            continue
        if state not in ('absent', 'update'):
            raise SetupRefused('not_pebbles', f'A file Pebble did not write appeared at {path}; it was left alone.')
        if role == 'launcher':
            ensure_private_dir(p['bridge'])
        else:
            os.makedirs(p['hosts'], mode=HOSTS_MODE, exist_ok=True)
        _write(path, data, mode)
        written.append(str(path))
    after = plan(data_home, user_home=user_home, program=program)
    return {**after, 'written': written}


def routes(app, *, user_home=None, program=None):
    """The two main-window routes for App.dispatch (the parent's app.py patch merges them)."""
    def where():
        if not getattr(getattr(app, 'storage', None), 'active', False):
            return None
        return Path(app.home), Path(user_home) if user_home is not None else Path.home(), \
            list(program) if program is not None else host_program()

    def status(**_):
        found = where()
        if found is None:
            return {'format': PLAN_FORMAT, 'available': False, 'browser': BROWSER, 'extension_id': EXTENSION_ID,
                    'files': [], 'installed': False, 'ready': False,
                    'problems': [{'code': 'not_pebble_home', 'message': NOT_HERE}], 'plan_sha256': None,
                    'never': list(NEVER), 'authorization': 'none'}
        home, user, command = found
        return plan(home, user_home=user, program=command)

    def connect(confirm=None, **_):
        found = where()
        if found is None:
            raise SetupRefused('not_pebble_home', NOT_HERE)
        home, user, command = found
        return install(home, user_home=user, program=command, confirm=confirm)
    return {'browser_connect_status': status, 'browser_connect_install': connect}
