"""Typed command plans: reviewed argv, pinned executables and narrowing-only admission.

A plan is plain canonical JSON. Every field is reviewed on its own: per-step
argv (a JSON list, never a shell string), the executable pinned by
recipes.pin, cwd, literal allowlisted environment, writable project subpaths,
destination-scoped network with credential references, resources, process
model, interactivity and on_disconnect. plan_hash is the SHA-256 of the
canonical plan without plan_hash; it detects change and authenticates nothing.

Plans carry no authority. The broker's durable action payload stays the
reviewed record: admission compares a requested plan with narrow(), which only
narrows, and verify() re-pins every executable and re-checks the root
immediately before spawn. Command text and argv are untrusted data parsed
through terminal.inspect_command, so the same commands stay refused. There is
no shell: a linear pipeline is 2-4 steps joined by host-created pipes, and
redirections are refused (deferred).
"""

import hashlib
import ipaddress
import json
import re
import shlex
import stat
import threading
import unicodedata
from pathlib import Path, PurePosixPath

from . import egress, recipes
from .policy import PRIVATE, PolicyError, Workspace
from .terminal import BLOCKED, inspect_command, resolve_executable

VERSION = 1
MAX_STEPS = 4
MAX_ARGV = 256
MAX_ARG = 4000
MAX_WRITABLE = 16
MAX_ENV = 16
ENV_VALUE_LIMIT = 1024
MAX_CREDENTIALS = 8
PATH_LIMIT = 512
PREFIX_LIMIT = 256
ROOT_LIMIT = 4096
PLAN_LIMIT = 32_768  # canonical bytes; a plan rides inside the 65,536-byte process payload
HTTPS_PORT = 443
KEYS = frozenset({'plan_version', 'steps', 'pipeline', 'writable', 'network', 'resources', 'process_model',
                  'interactive', 'on_disconnect', 'root', 'root_identity', 'risk', 'plan_hash'})
STEP_KEYS = frozenset({'argv', 'executable', 'cwd', 'env', 'stdin'})
STEP_INPUT = frozenset({'argv', 'cwd', 'env', 'stdin'})  # the host pins the executable itself
FIELDS = frozenset({'pipeline', 'writable', 'network', 'resources', 'process_model', 'interactive', 'on_disconnect'})
NETWORK_KEYS = frozenset({'mode', 'destinations', 'credentials'})
CREDENTIAL_KEYS = frozenset({'ref', 'destination', 'methods', 'path_prefix'})
METHODS = frozenset({'DELETE', 'GET', 'HEAD', 'PATCH', 'POST', 'PUT'})
PIPELINES = ('none', 'linear')
# Ascending authority: narrow() admits a value at or left of the reviewed one.
STDIN = ('none', 'pty')
PROCESS_MODELS = ('single', 'tree')
DISCONNECT = ('terminate', 'continue')
RISKS = ('read-only', 'execution', 'dangerous', 'network')  # terminal.inspect_command classes
# (minimum, default, maximum). Defaults are conservative: today's 60 s synchronous deadline (300 s
# when asynchronous), CPU seconds per process equal to the wall timeout, 2 GB measured footprint,
# 64 extra processes for the UID, the macOS default 256 descriptors, 256 MB per file, 1 GB written
# and the existing 192,000-byte stored output bound.
RESOURCES = {
    'timeout': (1, 60, 300),
    'cpu_seconds': (1, 60, 1200),
    'memory_mb': (64, 2048, 16384),
    'nproc': (1, 64, 512),
    'nofile': (16, 256, 4096),
    'fsize_mb': (1, 256, 4096),
    'disk_write_mb': (1, 1024, 16384),
    'output_bytes': (1, 192_000, 192_000),
}
DEFAULTS = {key: bounds[1] for key, bounds in RESOURCES.items()}
ASYNC_TIMEOUT = 300
# Literal values only. Host-owned keys (PATH, HOME, TMPDIR), loader and interpreter injection
# (DYLD_*, PYTHONPATH, NODE_OPTIONS, ...), proxies and every secret stay out of plans.
ENV_KEYS = frozenset({'CI', 'COLUMNS', 'FORCE_COLOR', 'LANG', 'LC_ALL', 'LC_CTYPE', 'LINES', 'NO_COLOR', 'NODE_ENV',
                      'PYTHONDONTWRITEBYTECODE', 'PYTHONHASHSEED', 'PYTHONIOENCODING', 'PYTHONUNBUFFERED',
                      'RUST_BACKTRACE', 'TERM', 'TZ'})
# C0/C1 controls (NUL, newline, tab, ESC), DEL, bidi and zero-width format characters, unassigned,
# private-use and surrogate code points, and every separator except the plain space.
TEXT_REFUSED = frozenset({'Cc', 'Cf', 'Cn', 'Co', 'Cs', 'Zl', 'Zp', 'Zs'})
# A command string is unambiguous only in this alphabet: shlex and any POSIX shell split it alike.
SAFE_COMMAND = re.compile(r'[A-Za-z0-9@%+=:,./_ -]+')
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
# Segments never start with a dot and are separated by exactly one slash, so matching is linear.
PREFIX = re.compile(r'/(?:[A-Za-z0-9_~-][A-Za-z0-9._~-]*(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)*/?)?')
MARKER = re.compile(r'[A-Za-z0-9_-]{8,128}')
# inspect_command refuses BLOCKED and these by argv[0]. Plans also refuse them by the pinned
# realpath's name (a renamed link to /bin/zsh) and add shells and wrappers that would run an
# unpinned program.
WRAPPERS = frozenset({'env', 'xargs', 'eval', 'exec', 'nohup', 'git'})
EXTRA = frozenset({'tcsh', 'ksh', 'dash', 'rbash', 'sudoedit', 'login', 'xcrun', 'arch', 'time', 'nice', 'caffeinate',
                   'sandbox-exec', 'script', 'open', 'chroot', 'command', 'builtin', 'timeout', 'gtimeout', 'watch',
                   'expect'})
SYSTEM_BIN = (Path('/bin'), Path('/sbin'), Path('/usr/bin'), Path('/usr/sbin'))
# Also compared by content, so a copied shell under another name is refused. Developer-tool shims
# in /usr/bin (git, make, python3, xcrun) can share one body and are deliberately not listed.
COPIED = ('bash', 'sh', 'zsh', 'csh', 'tcsh', 'ksh', 'dash', 'fish', 'su', 'sudo', 'doas', 'launchctl', 'osascript',
          'security', 'diskutil', 'fdisk', 'dd', 'chmod', 'chown', 'chflags', 'shutdown', 'reboot', 'halt', 'env',
          'xargs', 'nohup')
DIGEST_LIMIT = 64 * 1024 * 1024
# Toolchain roots that would make exec and reads system-wide.
BROAD = frozenset(map(Path, ('/usr', '/opt', '/var', '/etc', '/tmp', '/System', '/Library', '/Applications',
                             '/Users', '/Volumes', '/private', '/private/var', '/private/tmp', '/private/etc')))
NO_NETWORK = {'mode': 'none', 'destinations': [], 'credentials': []}
PRIVILEGED = ('Privileged commands, shell wrappers and system administration are blocked. '
              'Run them yourself outside Pebble.')  # inspect_command's own refusal (Phase 46: a shell's adds SHELL_HINT)
AMBIGUOUS = ('Quotes, globs, escapes, ~ and other shell syntax make a command string ambiguous. Use only letters, '
             'digits, spaces and @%+=:,./_- in a command string, or propose the argv as a typed plan.')
DESTINATIONS = 'Name each network destination as an exact [host, 443] pair; open network is not a plan field.'
STEPS = 'A plan runs one step, or a linear pipeline of 2 to 4 steps.'

_DIGESTS = {}
_DIGEST_LOCK = threading.Lock()


def canonical(value):
    return recipes.canonical(value)


def plan_digest(plan):
    """SHA-256 of the canonical plan without plan_hash. Use it to seal a narrowed copy."""
    if not isinstance(plan, dict):
        raise PolicyError('The command plan must be a JSON object.')
    try:
        text = canonical({key: value for key, value in plan.items() if key != 'plan_hash'})
    except (TypeError, ValueError, RecursionError):
        raise PolicyError('The command plan must be plain JSON data.') from None
    return hashlib.sha256(text.encode()).hexdigest()


# Field shapes. Each returns the canonical form; validate() requires the input to equal it.

def _plain(text):
    return all(c == ' ' or unicodedata.category(c) not in TEXT_REFUSED for c in text)


def _argv(argv, where):
    if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV:
        raise PolicyError(f'{where} argv must be a list of 1 to {MAX_ARGV} strings.')
    for arg in argv:
        if not isinstance(arg, str) or len(arg) > MAX_ARG or not _plain(arg):
            raise PolicyError(f'{where} argv elements must be plain text without NUL, newline, control, '
                              'escape or bidirectional characters.')
    if not argv[0]:
        raise PolicyError(f'{where} needs an executable.')
    return list(argv)


def _privileged(name):
    name = name.casefold()
    bare = re.sub(r'[-_.]?\d[\d.]*$', '', name)
    return any(n in BLOCKED or n.startswith('mkfs') or n in WRAPPERS or n in EXTRA for n in (name, bare))


def _private(part):
    lower = part.lower()  # policy.Workspace.path's rule
    return lower in PRIVATE or lower.startswith('.env') or lower.endswith(('.pem', '.key', '.p12', '.pfx', '.gguf'))


def _relative(name, field):
    """A normalized project subpath; '.' is the whole project."""
    if (not isinstance(name, str) or not 1 <= len(name) <= PATH_LIMIT or not _plain(name) or '\\' in name
            or name.startswith('/')):
        raise PolicyError(f'The {field} must be a relative project path.')
    if name == '.':
        return name
    parts = PurePosixPath(name).parts
    if '..' in parts or '.' in parts or str(PurePosixPath(name)) != name:
        raise PolicyError(f'The {field} must be a normalized path inside the project, without "..".')
    if any(_private(part) for part in parts):
        raise PolicyError('Private credentials and model weights are outside tool access.')
    return name


def _covers(outer, inner):
    return outer == '.' or inner == outer or inner.startswith(outer + '/')


def _writable(value):
    if not isinstance(value, list) or len(value) > MAX_WRITABLE:
        raise PolicyError(f'Writable paths are a list of at most {MAX_WRITABLE} project subpaths.')
    names = sorted({_relative(name, 'writable path') for name in value})
    return [name for name in names if not any(other != name and _covers(other, name) for other in names)]


def _env(value, where):
    if not isinstance(value, dict) or len(value) > MAX_ENV:
        raise PolicyError(f'{where} environment is an object of at most {MAX_ENV} literal values.')
    for key, item in value.items():
        if key not in ENV_KEYS:
            raise PolicyError(f'{where} environment may set only ' + ', '.join(sorted(ENV_KEYS))
                              + '. Paths, loaders, interpreter options, proxies and secrets are host-owned.')
        if not isinstance(item, str) or len(item) > ENV_VALUE_LIMIT or not _plain(item):
            raise PolicyError(f'{where} environment values are plain literal text up to {ENV_VALUE_LIMIT} characters.')
    return dict(sorted(value.items()))


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _destination(item, loopback):
    if not isinstance(item, (list, tuple)) or len(item) != 2:
        raise PolicyError('Each network destination is an exact [host, port] pair.')
    host, port = egress.host_name(item[0], loopback), egress.port_number(item[1])
    if _is_ip(host):  # host_name admits only loopback literals, and only with the explicit test flag
        return [host, port]
    if host == 'localhost' or host.endswith(('.localhost', '.local')) or '.' not in host:
        raise PolicyError('Network destinations are fully qualified public host names.')
    if port != HTTPS_PORT:
        raise PolicyError('Network destinations are exact HTTPS hosts on port 443.')
    return [host, port]


def _credential(item, destinations, loopback):
    if not isinstance(item, dict) or set(item) != CREDENTIAL_KEYS:
        raise PolicyError('Each credential reference has exactly ref, destination, methods and path_prefix.')
    ref, methods, prefix = item['ref'], item['methods'], item['path_prefix']
    if not isinstance(ref, str) or not REF.fullmatch(ref):
        raise PolicyError('Credential references are short vault identifiers, never secret values.')
    destination = _destination(item['destination'], loopback)
    if destination not in destinations:
        raise PolicyError("A credential must reference one of the plan's listed network destinations.")
    if (not isinstance(methods, list) or not 1 <= len(methods) <= len(METHODS)
            or not all(isinstance(m, str) and m in METHODS for m in methods)):
        raise PolicyError('Credential methods are drawn from ' + ', '.join(sorted(METHODS)) + '.')
    if not isinstance(prefix, str) or len(prefix) > PREFIX_LIMIT or not PREFIX.fullmatch(prefix):
        raise PolicyError('Credential path prefixes are plain absolute URL paths without dot segments, '
                          'encodings, queries or fragments.')
    return {'ref': ref, 'destination': destination, 'methods': sorted(set(methods)), 'path_prefix': prefix}


def _network(value, loopback):
    if not isinstance(value, dict) or set(value) != NETWORK_KEYS:
        raise PolicyError('Network has exactly mode, destinations and credentials.')
    mode, destinations, credentials = value['mode'], value['destinations'], value['credentials']
    if not isinstance(destinations, list) or not isinstance(credentials, list):
        raise PolicyError('Network destinations and credentials are lists.')
    if mode == 'none':
        if destinations or credentials:
            raise PolicyError('A plan without network lists no destinations or credentials.')
        return {'mode': 'none', 'destinations': [], 'credentials': []}
    if mode != 'broker':
        raise PolicyError("Network mode is 'none' or 'broker'.")
    if not 1 <= len(destinations) <= egress.MAX_ALLOW or len(credentials) > MAX_CREDENTIALS:
        raise PolicyError(f'Broker network lists 1 to {egress.MAX_ALLOW} destinations and at most '
                          f'{MAX_CREDENTIALS} credential references.')
    clean = [list(pair) for pair in sorted({tuple(_destination(item, loopback)) for item in destinations})]
    grants = {canonical(c): c for c in (_credential(item, clean, loopback) for item in credentials)}
    return {'mode': 'broker', 'destinations': clean, 'credentials': [grants[key] for key in sorted(grants)]}


def _resources(value):
    if not isinstance(value, dict) or set(value) != set(RESOURCES):
        raise PolicyError('Resources have exactly ' + ', '.join(RESOURCES) + '.')
    for key, (low, _, high) in RESOURCES.items():
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise PolicyError(f'Choose resources.{key} from {low} to {high}.')
    return {key: value[key] for key in RESOURCES}


def _identity(identity, where):
    if not (recipes._identity_ok(identity) and identity['path'] == identity['realpath']
            and Path(identity['path']).is_absolute() and not recipes._unsafe(identity['path'])):
        raise PolicyError(f'{where} executable must be a pinned identity of an absolute realpath.')
    return dict(identity)


def _step(step, index, interactive):
    where = f'Step {index + 1}'
    if not isinstance(step, dict) or set(step) != STEP_KEYS:
        raise PolicyError(f'{where} has exactly argv, executable, cwd, env and stdin.')
    argv = _argv(step['argv'], where)
    identity = _identity(step['executable'], where)
    if _privileged(Path(argv[0]).name) or _privileged(Path(identity['realpath']).name):
        raise PolicyError(PRIVILEGED)
    if step['stdin'] not in STDIN:
        raise PolicyError(f"{where} stdin is 'none' or 'pty'.")
    if (step['stdin'] == 'pty') != interactive:
        raise PolicyError('Only an interactive plan has a PTY, and an interactive plan always has one.')
    return {'argv': argv, 'executable': identity, 'cwd': _relative(step['cwd'], f'{where} cwd'),
            'env': _env(step['env'], where), 'stdin': step['stdin']}


def validate(plan, *, loopback=False):
    """Strict structural check; returns the canonical plan with its plan_hash re-checked.

    Pure: it reads no files. loopback is the explicit test flag that admits
    loopback literal destinations; production never sets it.
    """
    if type(loopback) is not bool:
        raise PolicyError('loopback must be a boolean.')
    if not isinstance(plan, dict) or set(plan) != KEYS:
        raise PolicyError('The command plan must have exactly the plan v1 fields.')
    try:
        text = canonical({key: value for key, value in plan.items() if key != 'plan_hash'})
    except (TypeError, ValueError, RecursionError):
        raise PolicyError('The command plan must be plain JSON data.') from None
    if len(text) > PLAN_LIMIT:
        raise PolicyError(f'The command plan exceeds the {PLAN_LIMIT}-byte limit.')
    data = json.loads(text)
    if type(data['plan_version']) is not int or data['plan_version'] != VERSION:
        raise PolicyError('This command plan version is not supported.')
    if type(data['interactive']) is not bool:
        raise PolicyError('interactive is a boolean.')
    pipeline, steps = data['pipeline'], data['steps']
    if pipeline not in PIPELINES:
        raise PolicyError("pipeline is 'none' or 'linear'.")
    if not isinstance(steps, list) or not (len(steps) == 1 if pipeline == 'none' else 2 <= len(steps) <= MAX_STEPS):
        raise PolicyError(STEPS)
    if data['interactive'] and pipeline != 'none':
        raise PolicyError('A pipeline cannot be interactive.')
    root, identity = data['root'], data['root_identity']
    if (not isinstance(root, str) or not 1 < len(root) <= ROOT_LIMIT or not root.startswith('/') or not _plain(root)
            or not isinstance(identity, list) or len(identity) != 2
            or any(type(v) is not int or v < 0 for v in identity)):
        raise PolicyError('The command plan root is malformed.')
    if data['process_model'] not in PROCESS_MODELS:
        raise PolicyError("process_model is 'single' or 'tree'.")
    if data['on_disconnect'] not in DISCONNECT:
        raise PolicyError("on_disconnect is 'terminate' or 'continue'.")
    clean = {'plan_version': VERSION,
             'steps': [_step(step, index, data['interactive']) for index, step in enumerate(steps)],
             'pipeline': pipeline, 'writable': _writable(data['writable']),
             'network': _network(data['network'], loopback), 'resources': _resources(data['resources']),
             'process_model': data['process_model'], 'interactive': data['interactive'],
             'on_disconnect': data['on_disconnect'], 'root': root, 'root_identity': identity, 'risk': data['risk']}
    if clean['risk'] not in RISKS or (clean['risk'] == 'network') != (clean['network']['mode'] == 'broker'):
        raise PolicyError('The plan risk does not match its command and network mode.')
    if canonical(clean) != text:
        raise PolicyError('The command plan is not in canonical form (sorted, de-duplicated lists).')
    if plan['plan_hash'] != hashlib.sha256(text.encode()).hexdigest():
        raise PolicyError('The command plan hash does not match its fields.')
    return {**clean, 'plan_hash': plan['plan_hash']}


# Filesystem facts, re-checked at proposal, before spawn and when building profile inputs.

def _workspace(root):
    try:
        return Workspace(root)
    except (OSError, RuntimeError, TypeError):
        raise PolicyError('Select an existing project folder.') from None


def _locate(ws, name, field, folder):
    """The resolved path of a project subpath; a link in any component refuses."""
    if name == '.':
        return ws.root
    cursor = ws.root
    for part in PurePosixPath(name).parts:
        cursor = cursor / part
        if cursor.is_symlink() or cursor.is_junction():
            raise PolicyError(f'Symbolic links are outside command plans ({field}).')
    try:
        real = cursor.resolve(strict=True)
        info = real.stat()
    except (OSError, RuntimeError):
        raise PolicyError(f'The {field} must exist in the selected project.') from None
    if real != cursor or not real.is_relative_to(ws.root):
        raise PolicyError(f'The {field} escapes the selected project.')
    if folder and not stat.S_ISDIR(info.st_mode):
        raise PolicyError(f'The {field} must be a project folder.')
    if not stat.S_ISDIR(info.st_mode) and not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
        raise PolicyError(f'The {field} must be a project folder or a regular file without other links.')
    return real


def _risk(steps, root, network):
    """inspect_command's class for each step's exact argv; its refusals are its own."""
    rank = 0
    for index, step in enumerate(steps):
        try:
            checked = inspect_command(shlex.join(step['argv']), root, network)
        except (OSError, RuntimeError):
            raise PolicyError('Select an existing project folder.') from None
        if checked['argv'] != step['argv']:
            raise PolicyError(f'Step {index + 1} argv does not survive the command parser unchanged.')
        rank = max(rank, RISKS.index(checked['risk']))
    return RISKS[rank]


def _system_digests():
    """SHA-256 of the system shells and privileged tools present and readable, cached by stat."""
    found = set()
    with _DIGEST_LOCK:
        for folder in SYSTEM_BIN:
            for name in COPIED:
                path = folder / name
                try:
                    info = path.stat()
                    if not stat.S_ISREG(info.st_mode) or info.st_size > DIGEST_LIMIT:
                        continue
                    key = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    cached = _DIGESTS.get(str(path))
                    if cached is None or cached[0] != key:
                        with path.open('rb') as source:
                            cached = _DIGESTS[str(path)] = (key, hashlib.file_digest(source, 'sha256').hexdigest())
                except OSError:
                    continue
                found.add(cached[1])
    return found


def _provenance(identity, where):
    if _privileged(Path(identity['realpath']).name):
        raise PolicyError(PRIVILEGED)
    if identity['sha256'] in _system_digests():
        raise PolicyError(f'{where} executable is a copy of a shell or privileged system tool. ' + PRIVILEGED)


def _pin(argv0, ws, where):
    """Resolve argv[0] as the terminal broker does, then pin its realpath outside the project."""
    real = resolve_executable(argv0)
    if real == ws.root or real.is_relative_to(ws.root):
        raise PolicyError(f'{where} executable is inside the selected project. Run project scripts through '
                          'an installed runtime, e.g. python3 script.py.')
    identity = recipes.pin(str(real), ws.root)
    _provenance(identity, where)
    return identity


def _toolchain(real, project):
    """The exec and read root of one pinned executable; None for a system binary that needs none."""
    real = Path(real)
    if real.parent in SYSTEM_BIN:
        # /usr/bin developer tools are shims into the Command Line Tools.
        if real.parent == Path('/usr/bin') and recipes.DEVELOPER.is_dir():
            return recipes._confined(recipes.DEVELOPER, project)
        return None
    folder = recipes._toolchain(real)
    if folder in BROAD:
        raise PolicyError('Cannot establish a confined command runtime.')
    return recipes._confined(folder, project)


def _overlap(a, b):
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _drift(field):
    raise PolicyError(f'The command plan changed after review ({field}). Propose it again.')


def _bound_root(plan):
    ws = _workspace(plan['root'])
    if str(ws.root) != plan['root']:
        _drift('root')
    if list(ws.identity) != plan['root_identity']:
        _drift('root_identity')
    return ws


# Construction.

def _build(steps, root, fields, loopback):
    if type(loopback) is not bool:
        raise PolicyError('loopback must be a boolean.')
    unknown = sorted(str(key)[:32] for key in set(fields) - FIELDS)
    if unknown:
        raise PolicyError('Unknown command plan fields: ' + ', '.join(unknown[:8]) + '.')
    ws = _workspace(root)
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise PolicyError(STEPS)
    network = fields.get('network', NO_NETWORK)
    if isinstance(network, dict):
        network = {'destinations': [], 'credentials': [], **network}
    network = _network(network, loopback)
    built = []
    for index, step in enumerate(steps):
        where = f'Step {index + 1}'
        if not isinstance(step, dict) or 'argv' not in step or set(step) - STEP_INPUT:
            raise PolicyError(f'{where} has argv and optionally cwd, env and stdin; the host pins the executable.')
        cwd = _relative(step.get('cwd', '.'), f'{where} cwd')
        built.append({'argv': _argv(step['argv'], where), 'executable': None, 'cwd': cwd,
                      'env': step.get('env', {}), 'stdin': step.get('stdin', 'none')})
    # inspect_command's refusals come before anything is resolved or hashed.
    risk = _risk(built, ws.root, network['mode'] != 'none')
    for index, step in enumerate(built):
        if _privileged(Path(step['argv'][0]).name):
            raise PolicyError(PRIVILEGED)
        _locate(ws, step['cwd'], f'Step {index + 1} cwd', folder=True)
        step['executable'] = _pin(step['argv'][0], ws, f'Step {index + 1}')
    writable = _writable(fields.get('writable', []))
    for name in writable:
        _locate(ws, name, 'writable path', folder=False)
    resources = fields.get('resources', {})
    if not isinstance(resources, dict) or set(resources) - set(RESOURCES):
        raise PolicyError('Resources may set only ' + ', '.join(RESOURCES) + '.')
    plan = {'plan_version': VERSION, 'steps': built, 'pipeline': fields.get('pipeline', 'none'), 'writable': writable,
            'network': network, 'resources': {**DEFAULTS, **resources},
            'process_model': fields.get('process_model', 'tree'), 'interactive': fields.get('interactive', False),
            'on_disconnect': fields.get('on_disconnect', 'terminate'), 'root': str(ws.root),
            'root_identity': list(ws.identity), 'risk': risk}
    plan['plan_hash'] = plan_digest(plan)
    plan = validate(plan, loopback=loopback)
    for step in plan['steps']:
        _toolchain(step['executable']['realpath'], ws.root)
    return plan


def from_command(command, root, network=False, writable=False, asynchronous=False):
    """A one-step plan from a command string, parsed by terminal.inspect_command.

    network is False or a list of exact [host, 443] destinations (broker mode);
    writable is a bool (True: the whole project) or a list of project subpaths.
    """
    if network is True:
        raise PolicyError(DESTINATIONS)
    if (type(asynchronous) is not bool or not (type(writable) is bool or isinstance(writable, list))
            or not (network is False or isinstance(network, list))):
        raise PolicyError('Command plan options are typed: network is False or a destination list, writable a '
                          'boolean or path list, asynchronous a boolean.')
    try:
        checked = inspect_command(command, root, bool(network))
    except (OSError, RuntimeError):
        raise PolicyError('Select an existing project folder.') from None
    words = command.split(' ')
    if (not SAFE_COMMAND.fullmatch(command) or any(word.startswith('=') for word in words)
            or checked['argv'] != [word for word in words if word]):
        raise PolicyError(AMBIGUOUS)
    timeout = ASYNC_TIMEOUT if asynchronous else DEFAULTS['timeout']
    return _build([{'argv': checked['argv']}], root, {
        'writable': ['.'] if writable is True else writable or [],
        'network': {'mode': 'broker' if network else 'none', 'destinations': network or [], 'credentials': []},
        'resources': {'timeout': timeout, 'cpu_seconds': timeout}}, False)


def from_argv(steps, root, *, loopback=False, **fields):
    """The typed entry: steps [{argv, cwd?, env?, stdin?}] plus optional plan fields, each validated."""
    return _build(steps, root, fields, loopback)


# Admission and launch.

def _within(inner, outer):
    return inner == outer or inner.startswith(outer) and (outer.endswith('/') or inner[len(outer)] == '/')


def _grants(request, review):
    return (request['ref'] == review['ref'] and request['destination'] == review['destination']
            and set(request['methods']) <= set(review['methods'])
            and _within(request['path_prefix'], review['path_prefix']))


def narrow(reviewed, requested, *, loopback=False):
    """requested, only when every field equals or narrows reviewed; a refusal names the field."""
    old, new = validate(reviewed, loopback=loopback), validate(requested, loopback=loopback)

    def refuse(field):
        raise PolicyError(f'The requested command plan widens or changes the reviewed plan ({field}). '
                          'Propose it again for review.')

    for key in ('plan_version', 'root', 'root_identity', 'pipeline'):
        if canonical(old[key]) != canonical(new[key]):
            refuse(key)
    if new['interactive'] and not old['interactive']:
        refuse('interactive')
    for key, order in (('process_model', PROCESS_MODELS), ('on_disconnect', DISCONNECT)):
        if order.index(new[key]) > order.index(old[key]):
            refuse(key)
    if len(new['steps']) != len(old['steps']):
        refuse('steps')
    for index, (a, b) in enumerate(zip(old['steps'], new['steps'])):
        for key in ('argv', 'executable', 'cwd'):
            if canonical(a[key]) != canonical(b[key]):
                refuse(f'steps[{index}].{key}')
        if any(a['env'].get(key) != value for key, value in b['env'].items()):
            refuse(f'steps[{index}].env')
        if STDIN.index(b['stdin']) > STDIN.index(a['stdin']):
            refuse(f'steps[{index}].stdin')
    if not all(any(_covers(outer, name) for outer in old['writable']) for name in new['writable']):
        refuse('writable')
    if new['network']['mode'] == 'broker' and old['network']['mode'] == 'none':
        refuse('network.mode')
    if any(pair not in old['network']['destinations'] for pair in new['network']['destinations']):
        refuse('network.destinations')
    if not all(any(_grants(c, r) for r in old['network']['credentials']) for c in new['network']['credentials']):
        refuse('network.credentials')
    for key in RESOURCES:
        if new['resources'][key] > old['resources'][key]:
            refuse('resources.' + key)
    if new['network']['mode'] == old['network']['mode']:
        if new['risk'] != old['risk']:
            refuse('risk')
    elif new['risk'] != _risk(new['steps'], new['root'], False):
        refuse('risk')
    return new


def verify(plan, root, *, loopback=False):
    """Immediately before spawn: re-pin every executable and re-check root, cwd, writable and risk."""
    plan = validate(plan, loopback=loopback)
    ws = _workspace(root)
    if str(ws.root) != plan['root']:
        _drift('root')
    if list(ws.identity) != plan['root_identity']:
        _drift('root_identity')
    for index, step in enumerate(plan['steps']):
        field, expected = f'steps[{index}]', step['executable']
        try:
            current = recipes.pin(expected['path'], ws.root)
        except PolicyError as exc:
            raise PolicyError(f'The command plan changed after review ({field}.executable). {exc} '
                              'Propose it again.') from None
        changed = [key for key in recipes.IDENTITY if current[key] != expected[key]]
        if changed:
            _drift(f'{field}.executable: ' + ', '.join(changed))
        _provenance(current, f'Step {index + 1}')
        _toolchain(current['realpath'], ws.root)
        try:
            _locate(ws, step['cwd'], 'cwd', folder=True)
        except PolicyError:
            _drift(f'{field}.cwd')
    for name in plan['writable']:
        try:
            _locate(ws, name, 'writable path', folder=False)
        except PolicyError:
            _drift('writable: ' + name)
    if _risk(plan['steps'], ws.root, plan['network']['mode'] != 'none') != plan['risk']:
        _drift('risk')
    return plan


def review_view(plan, *, loopback=False):
    """Plain data for the native card. argv stays a JSON list per step, never a shell string."""
    plan = validate(plan, loopback=loopback)
    return {
        'plan_hash': plan['plan_hash'], 'root': plan['root'], 'risk': plan['risk'], 'pipeline': plan['pipeline'],
        'steps': [{'argv': list(step['argv']),
                   'executable': {key: step['executable'][key] for key in ('path', 'realpath', 'sha256', 'size')},
                   'cwd': step['cwd'], 'env': dict(step['env']), 'stdin': step['stdin']} for step in plan['steps']],
        'writable': list(plan['writable']),
        'network': {'mode': plan['network']['mode'],
                    'destinations': [list(pair) for pair in plan['network']['destinations']],
                    'credentials': [dict(item, destination=list(item['destination']), methods=list(item['methods']))
                                    for item in plan['network']['credentials']]},
        'resources': dict(plan['resources']), 'process_model': plan['process_model'],
        'interactive': plan['interactive'], 'on_disconnect': plan['on_disconnect'], 'authorization': 'none',
    }


def _temp(temp, project):
    if not isinstance(temp, (str, Path)) or not Path(temp).is_absolute() or recipes._unsafe(str(temp)):
        raise PolicyError('The job temporary folder must be a plain absolute path.')
    try:
        real = Path(temp).resolve(strict=True)
    except (OSError, RuntimeError):
        raise PolicyError('The job temporary folder is unavailable.') from None
    if (not real.is_dir() or real == Path(real.anchor) or Path.home().resolve().is_relative_to(real)
            or _overlap(real, project)):
        raise PolicyError('The job temporary folder must be its own folder, outside the project.')
    return real


def profile_inputs(plan, temp, job_marker, *, loopback=False):
    """Keyword arguments for sandbox_profiles.build; every path absolute, resolved and checked.

    Executables run only from pinned toolchains: never from the project, a
    writable path or temp. The caller adds broker_port for broker network.
    """
    plan = validate(plan, loopback=loopback)
    if not isinstance(job_marker, str) or not MARKER.fullmatch(job_marker):
        raise PolicyError('The job marker is 8 to 128 letters, digits, _ or -.')
    ws = _bound_root(plan)
    temp = _temp(temp, ws.root)
    writable = [_locate(ws, name, 'writable path', folder=False) for name in plan['writable']]
    toolchains = []
    for step in plan['steps']:
        real = Path(step['executable']['realpath'])
        folder = _toolchain(real, ws.root)
        if (_overlap(real, temp) or real.is_relative_to(ws.root)
                or folder is not None and (_overlap(folder, temp) or any(_overlap(folder, w) for w in writable))):
            raise PolicyError('Executables may run only from pinned toolchains, never from the project, '
                              'a writable path or the job temporary folder.')
        if folder is not None and str(folder) not in toolchains:
            toolchains.append(str(folder))
    return {'executable': plan['steps'][0]['executable']['realpath'], 'exec_paths': tuple(toolchains),
            'read_paths': tuple(toolchains), 'project_root': str(ws.root), 'temp': str(temp),
            'writable_paths': tuple(str(path) for path in writable), 'network': plan['network']['mode'],
            'process_model': plan['process_model'], 'job_marker': job_marker}


def legacy_payload(plan, *, loopback=False):
    """Today's command payload fields for a one-step plan, plus plan_hash, for process_jobs binding.

    A binding projection, not an execution vehicle: the legacy workers cannot
    enforce destination scoping, subpath writes, cwd, env, single-process or
    resource fields, so the plan itself must drive execution.
    """
    plan = validate(plan, loopback=loopback)
    if plan['pipeline'] != 'none':
        raise PolicyError('Only a one-step plan maps onto a terminal command payload.')
    argv, network = plan['steps'][0]['argv'], plan['network']['mode'] != 'none'
    command = shlex.join(argv)
    try:
        checked = inspect_command(command, plan['root'], network)
    except (OSError, RuntimeError):
        raise PolicyError('Select an existing project folder.') from None
    if (checked['argv'], checked['risk'], checked['root'], checked['root_identity']) != (
            argv, plan['risk'], plan['root'], plan['root_identity']):
        raise PolicyError('The command plan does not map onto an unchanged terminal command.')
    return {'command': command, 'argv': list(argv), 'root': plan['root'], 'root_identity': list(plan['root_identity']),
            'network': network, 'writable': bool(plan['writable']), 'risk': plan['risk'],
            'plan_hash': plan['plan_hash']}
