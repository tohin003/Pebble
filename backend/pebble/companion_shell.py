"""Phase 40 (1.6.0, D90-D92): the Companion's Shell, one command line inside a per-command macOS sandbox.

shell_policy decides first (deny, ask or allow). An allowed or approved line runs as `/bin/zsh -f -c <line>` under
`sandbox-exec` with a profile built for this call (sandbox_profiles.build):
- reads: the system set, /Applications, /Library, Homebrew and the home folder, minus the protected list (D92:
  keys, passwords, browser data, Messages, Mail, Pebble's own data, other agents' credentials, shell histories);
- writes: the workbench (~/Pebble Workbench) and the paths the person opened for this call or granted;
- exec: the system tool folders, Homebrew, framework Pythons and the person's toolchains, never the workbench;
- network: none, or the egress broker admitting only the approved hosts on 443 (it resolves names itself and
  refuses private, loopback and link-local addresses);
- a fresh environment (no keys, tokens or Pebble internals), a timeout, the whole process group killed at the end;
- before a call may change paths outside the workbench, those paths are cloned (APFS clonefile) so the change can be
  undone; a path that cannot be cloned is reported as not undoable before it runs.
Output is untrusted text: capped, stripped of terminal controls and returned with its hash.
"""

import ctypes
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from stat import S_ISDIR

from . import shell_policy
from .policy import PolicyError
from .process_exit import LeaderWatch
from .terminal_text import sanitize as sanitize_terminal
from .zone import person_zone

TIMEOUT_DEFAULT = 60
TIMEOUT_MAX = 300
CAPTURE_LIMIT = 400_000   # bytes kept from the log
SHOWN_LIMIT = 16_000      # bytes shown to the model, as RunCommand
SNAPSHOT_FILES = 20_000
SNAPSHOT_BYTES = 2 << 30
ZSH = Path('/bin/zsh')
SANDBOX_EXEC = Path('/usr/bin/sandbox-exec')
EXEC_ROOTS = ('/usr/sbin', '/sbin', '/opt/homebrew', '/usr/local', '/Library/Frameworks/Python.framework',
              '/Applications/Xcode.app/Contents/Developer', '/Library/Developer/CommandLineTools')
HOME_EXEC = ('.local/bin', '.cargo/bin', '.nvm/versions', '.pyenv/versions', '.rbenv/versions', 'go/bin',
             '.bun/bin', '.deno/bin', '.volta', 'Library/pnpm', '.rustup/toolchains')
READ_ROOTS = ('/Applications', '/Library', '/opt/homebrew', '/usr/local', '/private/tmp', '/private/var/folders')
PATH_DIRS = ('/opt/homebrew/bin', '/opt/homebrew/sbin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin')
# Secret file names, denied anywhere below the home folder (not system-wide: /etc/ssl/cert.pem is the CA bundle).
SBPL_SECRET_NAMES = (r'(\.env(\.[^/]*)?|\.envrc|credentials(\.json|\.toml|\.ya?ml)?|secrets?(\.json|\.toml|\.ya?ml)?'
                     r'|id_(rsa|dsa|ecdsa|ed25519)|[^/]*\.(pem|key|p12|pfx|kdbx|keychain|keychain-db|ppk|jks|keystore))$')
# Phase 46 (1.9.28, golden held-out companion_desktop: 2 false completions of 15 on Opus): the sandbox's refusal as each
# tool words it, measured under sandbox-exec on a throwaway home: "Operation not permitted" (sed, gzip, bzip2, xz,
# truncate, cp, perl -i, Python), Cocoa's "You don’t have permission" (textutil) and sips's "Cannot to rename temporary
# file" (exit 13). zsh's "permission denied: ./x.sh" is a file's mode, never the sandbox. The Cocoa wording counts only
# as textutil prints it, ending its error line (sec-w7 3: a page saying "You do not have permission to view this page"
# is no refusal).
REFUSED = re.compile(rb"operation not permitted|don(?:'|\xe2\x80\x99)t have permission\.?[ \t]*$|cannot to rename "
                     rb"temporary file", re.I | re.M)


def secret_names_rule(home):
    return '^' + re.escape(str(home)) + '/(.*/)?' + SBPL_SECRET_NAMES


def _real(path):
    return Path(os.path.realpath(path))


def ensure_workbench(home=None):
    bench = shell_policy.workbench(home)
    bench.mkdir(mode=0o700, exist_ok=True)
    return _real(bench)


def protected_paths(home=None):
    """Every protected folder or file that exists, resolved: the D92 table plus Pebble's live data folders."""
    from .data_home import protected_roots
    home = _real(home or Path.home())
    found = []
    for entry in shell_policy.PROTECTED_HOME:
        path = home / entry
        if os.path.lexists(path):
            found.append(_real(path))
    try:
        found += [_real(home / name) for name in os.listdir(home) if name.startswith(shell_policy.PROTECTED_PREFIX)]
    except OSError:
        pass
    for root in protected_roots():
        if os.path.lexists(root):
            found.append(_real(root))
    return sorted(set(found), key=str)


# Phase 46 (1.9.29): where xcode-select keeps its choice (newer macOS under /var/select, older under /var/db)
SELECTION_LINKS = ('/private/var/select/developer_dir', '/private/var/db/xcode_select_link')


def selected_developer():
    """Phase 46 (1.9.29): the developer folder xcode-select chose, when it is the Command Line Tools or an Xcode in
    /Applications whose folders down to usr/bin are root's and written by nobody else (git_workflows.confinement's
    rule); else None. Apple's /usr/bin shims run their tools from it. The Shell already reads /Applications and
    /Library; this lets them run from an Xcode of another name (Xcode-beta.app) as from Xcode.app, as in the person's
    own terminal. Without a link (macOS 26 keeps none until `xcode-select -s` runs; this Mac, 2026-10-30) xcode-select
    uses its default, Xcode.app or the Command Line Tools, which EXEC_ROOTS already holds."""
    for link in SELECTION_LINKS:
        try:
            developer = Path(os.readlink(link)).resolve()
        except (OSError, RuntimeError):
            continue
        xcode = (developer.name == 'Developer' and developer.parent.name == 'Contents'
                 and developer.parent.parent.suffix == '.app' and developer.parent.parent.parent == Path('/Applications'))
        if developer != Path('/Library/Developer/CommandLineTools') and not xcode:
            return None
        try:
            infos = [os.lstat(p) for p in (developer, developer / 'usr', developer / 'usr' / 'bin')]
        except OSError:
            return None
        trusted = all(S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022 for info in infos)
        return developer if trusted else None
    return None


def exec_paths(home=None):
    home = _real(home or Path.home())
    found = [Path(p) for p in EXEC_ROOTS if os.path.isdir(p)]
    found += [_real(home / p) for p in HOME_EXEC if os.path.isdir(home / p)]
    selected = selected_developer()  # Phase 46 (1.9.29): the shims' own tools, wherever xcode-select points
    if selected is not None:
        found.append(selected)
    return sorted(set(_real(p) for p in found), key=str)


def environment(home, temp, proxy=None):
    """A fresh environment: nothing inherited but the locale; no keys, tokens or Pebble internals."""
    env = {'PATH': ':'.join(d for d in PATH_DIRS if os.path.isdir(d)), 'HOME': str(home), 'TMPDIR': str(temp) + '/',
           # Phase 46 (1.9.27, golden dev d-cd-01 on sol): zsh writes here-document and =(...) temporary files under
           # TMPPREFIX (default /tmp/zsh), not TMPDIR, and the sandbox denies /tmp, so every heredoc failed with
           # "can't create temp file for here document: operation not permitted" (zsh 5.9, measured). The same
           # prefix inside the job's own temporary folder keeps them inside the sandbox.
           'TMPPREFIX': str(temp) + '/zsh',
           'USER': os.environ.get('USER', home.name), 'LOGNAME': os.environ.get('USER', home.name), 'SHELL': str(ZSH),
           'LANG': os.environ.get('LANG') or 'en_US.UTF-8', 'TERM': 'dumb', 'NO_COLOR': '1',
           'GIT_TERMINAL_PROMPT': '0', 'PYTHONDONTWRITEBYTECODE': '1', 'HOMEBREW_NO_AUTO_UPDATE': '1',
           **person_zone()}  # Phase 46: the person's TZ (zone.py)
    if proxy:
        for name in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy'):
            env[name] = proxy
        env['NO_PROXY'] = env['no_proxy'] = ''
    return env


# -- snapshots ---------------------------------------------------------------------------------------------------

def _clonefile(src, dst):
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.clonefile
    fn.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
    if fn(os.fsencode(src), os.fsencode(dst), 0) != 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))


def _size(path):
    files, total = 0, 0
    if path.is_file() or path.is_symlink():
        return 1, path.lstat().st_size
    for base, dirs, names in os.walk(path):
        files += len(names) + len(dirs)
        total += sum(os.lstat(os.path.join(base, n)).st_size for n in names)
        if files > SNAPSHOT_FILES or total > SNAPSHOT_BYTES:
            break
    return files, total


def snapshot(paths, store):
    """Clone each existing path into `store/<id>/<n>` before a call may change it. Returns a record: the snapshot id,
    each path with its clone (or the reason it could not be cloned: too big, another volume, missing)."""
    ident = uuid.uuid4().hex
    folder = Path(store) / ident
    folder.mkdir(parents=True, mode=0o700)
    entries = []
    for n, path in enumerate(paths):
        path = Path(path)
        if not os.path.lexists(path):
            entries.append({'path': str(path), 'existed': False, 'undoable': True})
            continue
        files, total = _size(path)
        if files > SNAPSHOT_FILES or total > SNAPSHOT_BYTES:
            entries.append({'path': str(path), 'existed': True, 'undoable': False, 'why': 'too big to keep a copy'})
            continue
        clone = folder / str(n)
        try:
            _clonefile(path, clone)
        except OSError as exc:
            entries.append({'path': str(path), 'existed': True, 'undoable': False, 'why': exc.strerror or 'clone failed'})
            continue
        entries.append({'path': str(path), 'existed': True, 'undoable': True, 'clone': str(clone)})
    record = {'id': ident, 'created': time.time(), 'entries': entries}
    (folder / 'snapshot.json').write_text(json.dumps(record, indent=1))
    return record


def restore(record):
    """Put every undoable path back as it was: a cloned path replaces what is there now; a path that did not exist
    is removed. Returns what was restored and what could not be."""
    restored, failed = [], []
    for entry in record['entries']:
        path = Path(entry['path'])
        if not entry.get('undoable'):
            failed.append({'path': str(path), 'why': entry.get('why', 'not undoable')})
            continue
        try:
            if os.path.lexists(path):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            if entry.get('existed'):
                _clonefile(entry['clone'], path)
            restored.append(str(path))
        except OSError as exc:
            failed.append({'path': str(path), 'why': exc.strerror or str(exc)})
    return {'restored': restored, 'failed': failed}


# -- running -----------------------------------------------------------------------------------------------------

def profile_for(*, home, bench, temp, writable, network, broker_port=None, keep_paths=(), undeletable_paths=()):
    from .sandbox_profiles import build
    reads = [Path(p) for p in READ_ROOTS if os.path.isdir(p)] + [home]
    return build(keep_paths=keep_paths, undeletable_paths=undeletable_paths,  # Phase 46 (1.9.28, b-new G2)
                 executable=ZSH, exec_paths=exec_paths(home), read_paths=reads, project_root=bench, temp=temp,
                 writable_paths=[bench, *writable], network=network, broker_port=broker_port, private_deny=False,
                 job_marker=uuid.uuid4().hex, process_model='tree', deny_paths=protected_paths(home),
                 deny_names=secret_names_rule(home))


def run(line, *, cwd=None, writable=(), hosts=(), timeout=TIMEOUT_DEFAULT, cancel=None, home=None,
        snapshot_store=None, snapshot_paths=(), keep_paths=(), undeletable_paths=()):
    """Run an allowed or approved line. `writable` are the paths opened for this call (grants included); `hosts` the
    approved hosts; `snapshot_paths` are cloned first when a snapshot store is given."""
    if not SANDBOX_EXEC.is_file() or not ZSH.is_file():
        raise PolicyError('The Shell needs macOS sandboxing, which is not available here.')
    if type(timeout) is not int or not 1 <= timeout <= TIMEOUT_MAX:
        raise PolicyError(f'Choose a timeout from 1 to {TIMEOUT_MAX} seconds.')
    home = _real(home or Path.home())
    bench = ensure_workbench(home)
    where = _real(os.path.expanduser(cwd)) if cwd else bench
    if not where.is_dir() or shell_policy.protected_path(where, home):
        raise PolicyError('Run the command in an existing, unprotected folder.')
    opened = []
    for value in writable:
        path, _ = shell_policy.writable_folder(str(value), home)
        opened.append(path)
    snap = snapshot(snapshot_paths, snapshot_store) if snapshot_store and snapshot_paths else None
    from .egress import EgressBroker
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='pebble-shell-') as temp:
        temp = _real(temp)
        broker = EgressBroker([(h, 443) for h in hosts]) if hosts else None
        try:
            if broker:
                broker.start()
            profile = profile_for(home=home, bench=bench, temp=temp, writable=opened,
                                  network='broker' if broker else 'none', broker_port=broker.port if broker else None,
                                  keep_paths=keep_paths, undeletable_paths=undeletable_paths)
            env = environment(home, temp, proxy=broker.url if broker else None)
            log_path = temp / 'output.log'
            with log_path.open('wb') as log:
                process = subprocess.Popen([str(SANDBOX_EXEC), '-p', profile, str(ZSH), '-f', '-c', line], cwd=where,
                                           env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                watch = LeaderWatch(process.pid)
                outcome = 'exited'
                try:
                    deadline = time.monotonic() + timeout
                    while not watch.exited(0.1 if watch.reliable else 0):
                        if cancel is not None and cancel.is_set():
                            outcome = 'cancelled'
                        elif time.monotonic() > deadline:
                            outcome = 'timed_out'
                        elif log_path.stat().st_size > CAPTURE_LIMIT * 4:
                            outcome = 'output_limit'
                        if outcome != 'exited':
                            break
                        if not watch.reliable:
                            if process.poll() is not None:
                                break
                            time.sleep(0.1)
                finally:
                    _sweep(process)
                    process.wait()
                    watch.close()
            raw = log_path.read_bytes()[:CAPTURE_LIMIT]
        finally:
            refused = broker.log() if broker else []
            if broker:
                broker.close()
    shown, removed, _ = sanitize_terminal(raw[:SHOWN_LIMIT], final=True)
    result = {
        'exit_code': process.returncode if outcome == 'exited' else None,
        'outcome': outcome,
        'output': shown,
        'output_truncated': len(raw) > SHOWN_LIMIT,
        'output_sha256': hashlib.sha256(raw).hexdigest(),
        'controls_removed': removed,
        'seconds': round(time.monotonic() - began, 2),
        'cwd': str(where),
        'isolation': 'macOS sandbox: writes in the workbench' + (' and ' + ', '.join(map(str, opened)) if opened else '')
                     + '; ' + ('network to ' + ', '.join(hosts) if hosts else 'no network'),
        'untrusted_output': True,
    }
    if hosts:
        result['network_refused'] = [r for r in refused if not r.get('allowed', True)][:8]
    if snap:
        result['snapshot'] = {'id': snap['id'], 'undoable': all(e['undoable'] for e in snap['entries']),
                              'not_undoable': [e['path'] for e in snap['entries'] if not e['undoable']]}
    if outcome != 'exited':
        result['note'] = {'timed_out': f'Stopped after {timeout} s; the outcome is uncertain.',
                          'cancelled': 'Stopped; the outcome is uncertain.',
                          'output_limit': 'Stopped: too much output; the outcome is uncertain.'}[outcome]
    elif process.returncode and REFUSED.search(raw):
        # Phase 46 (1.9.28): flagged whatever the exit status (the completion check reads it:
        # companion_tools.refusal_issues), and on every tool's wording of the refusal (REFUSED)
        result['sandbox_refused'] = True
        result['hint'] = ('The sandbox refused something. To write outside the workbench, call Shell again with '
                          'writable=[folder]; for the network, hosts=[host]. Pebble then asks the person.')
    elif REFUSED.search(raw):
        # Phase 46 (golden held-out companion_desktop, 2 false completions on Opus): `find ... -exec mv` exits 0 even
        # when every mv was refused (measured: nothing moved, exit 0), and the run reported the move done
        result['sandbox_refused'] = True
        result['hint'] = ('Exit status 0, but the sandbox refused at least one write ("Operation not permitted" above), '
                          'so part of this did not happen. Check what actually changed before saying it is done; to '
                          'write outside the workbench, call Shell again with writable=[folder] (Pebble asks the person).')
    return result


def _sweep(process):
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
