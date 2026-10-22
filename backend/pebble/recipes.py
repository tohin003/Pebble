"""Project recipes: a fixed registry, bounded detection and pinned executables.

Repository text is untrusted data. Detection reads only bounded root manifests
and the bounded workspace inventory. It never selects an executable, argument
or environment value outside a registered recipe; an npm script is the only
repository text a plan carries, recorded literally so the reviewer sees it.

A plan pins its executable by path, realpath, SHA-256, size, mtime, device,
inode, owner and mode. verify() re-checks that identity immediately before
launch and matches() re-derives every other field from the snapshot, so a plan
cannot widen reads, argv or environment after review. Plans and inspections
carry no authority: the host broker still decides whether anything runs.
"""

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from .policy import PolicyError

FRAMEWORK = Path('/Library/Frameworks/Python.framework')
PYTHONS = (FRAMEWORK / 'Versions/3.14/bin/python3', Path('/usr/bin/python3'))
DEVELOPER = Path('/Library/Developer/CommandLineTools')
GOS = (Path('/usr/local/go/bin/go'), Path('/opt/homebrew/bin/go'))
CARGOS = (Path('/opt/homebrew/bin/cargo'), Path('/usr/local/bin/cargo'))
# The /usr/bin/make shim execs xcrun, which writes a cache outside the sandbox
# temp folder. Pin the Command Line Tools make that actually runs instead.
MAKE = DEVELOPER / 'usr/bin/make'
# python.org framework installs are root:admin 0775. Members of admin (gid 80)
# already hold administrator rights, so that one group write bit on a root-owned
# file is accepted. Any other group write bit, and every world write bit, is refused.
TRUSTED_WRITER_GROUPS = frozenset({80})
KINDS = ('test', 'build', 'lint')
MOUNT_KINDS = ('node_modules', 'python-env')
MANIFESTS = ('package.json', 'pyproject.toml', 'setup.cfg', 'pytest.ini', 'go.mod', 'Cargo.toml', 'Makefile')
MANIFEST_LIMIT = 256 * 1024
SCRIPT_LIMIT = 4096
TIMEOUT_LIMIT = 300
OUTPUT_LIMIT = 192_000
EXECUTABLE_LIMIT = 512 * 1024 * 1024
AUTO = ('python-pytest', 'python-unittest', 'npm-test', 'node-test', 'go-test', 'cargo-test', 'make-test')
IDENTITY = ('path', 'realpath', 'sha256', 'size', 'mtime_ns', 'dev', 'ino', 'uid', 'mode')
PLAN_KEYS = frozenset({'recipe', 'kind', 'argv', 'cwd', 'executable', 'entrypoint', 'toolchain_reads', 'env',
                       'timeout', 'output_limit', 'network', 'script', 'mount'})
MOUNT_KEYS = frozenset({'kind', 'root', 'reads', 'links', 'executable', 'env', 'environment_id', 'tree_digest'})
MOUNT_ENV = frozenset({'VIRTUAL_ENV', 'NODE_PATH'})
TEMP = '{temp}'
NPM_CLI = '{npm-cli}'


@dataclass(frozen=True)
class Recipe:
    id: str
    kind: str
    language: str
    toolchain: str
    argv: tuple
    env: tuple
    timeout: int
    output_limit: int
    network: bool
    needs: Optional[str]
    detect: Callable


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


# Detection: pure functions over bounded facts. They return a reason or None.

def _base(name):
    return PurePosixPath(name).name


def _detect_unittest(facts):
    if any(re.fullmatch(r'test.*\.py', _base(n)) for n in facts['files']):
        return 'test*.py files'


def _detect_pytest(facts):
    texts = facts['texts']
    if texts.get('pyproject.toml') and re.search(r'^\[tool\.pytest', texts['pyproject.toml'], re.M):
        return 'pyproject.toml [tool.pytest]'
    if 'pytest.ini' in facts['present']:
        return 'pytest.ini'
    if texts.get('setup.cfg') and re.search(r'^\[tool:pytest\]', texts['setup.cfg'], re.M):
        return 'setup.cfg [tool:pytest]'
    if any(_base(n) == 'conftest.py' for n in facts['files']):
        return 'conftest.py'
    if any(re.fullmatch(r'test_.*\.py|.*_test\.py', _base(n)) for n in facts['files']):
        return 'test_*.py files'


NODE_TESTS = re.compile(r'(?:^|/)(?:[^/]+[.\-_]test|test-[^/]+|test)\.[cm]?js$|(?:^|/)test/(?:[^/]+/)*[^/]+\.[cm]?js$')


def _detect_node(facts):
    if any(NODE_TESTS.search(n) for n in facts['files']):
        return 'node --test files'


def _script_detector(name):
    def detect(facts):
        scripts = (facts['package'] or {}).get('scripts', {})
        if name in scripts:
            return f'package.json scripts.{name}'
    return detect


def _go_module(facts):
    return bool(facts['texts'].get('go.mod') and re.search(r'^module[ \t]+\S+', facts['texts']['go.mod'], re.M))


def _detect_go_test(facts):
    if _go_module(facts) and any(_base(n).endswith('_test.go') for n in facts['files']):
        return 'go.mod with *_test.go files'


def _detect_go(facts):
    if _go_module(facts):
        return 'go.mod'


def _detect_cargo(facts):
    if facts['texts'].get('Cargo.toml') and re.search(r'^\[(?:package|workspace)\]', facts['texts']['Cargo.toml'], re.M):
        return 'Cargo.toml'


MAKE_TEST = re.compile(r'^test[ \t]*:(?!=)', re.M)


def _detect_make(facts):
    if facts['texts'].get('Makefile') and MAKE_TEST.search(facts['texts']['Makefile']):
        return 'Makefile test: target'


GO_ENV = (('CGO_ENABLED', '0'), ('GOCACHE', TEMP + '/go-build'), ('GOENV', 'off'), ('GOFLAGS', '-mod=readonly'),
          ('GOPATH', TEMP + '/go'), ('GOPROXY', 'off'), ('GOTOOLCHAIN', 'local'), ('GOWORK', 'off'))
NPM_ENV = (('NO_UPDATE_NOTIFIER', '1'), ('npm_config_audit', 'false'), ('npm_config_cache', TEMP + '/npm-cache'),
           ('npm_config_fund', 'false'), ('npm_config_globalconfig', TEMP + '/npmrc-global'),
           ('npm_config_ignore_scripts', 'true'), ('npm_config_offline', 'true'), ('npm_config_script_shell', '/bin/sh'),
           ('npm_config_update_notifier', 'false'), ('npm_config_userconfig', TEMP + '/npmrc'))
CARGO_ENV = (('CARGO_NET_OFFLINE', 'true'), ('CARGO_TARGET_DIR', TEMP + '/cargo-target'), ('CARGO_TERM_COLOR', 'never'))


def _recipe(id, kind, language, toolchain, argv, env=(), timeout=120, needs=None, detect=None):
    if kind not in KINDS or not 0 < timeout <= TIMEOUT_LIMIT or needs not in (None, *MOUNT_KINDS) or not callable(detect):
        raise ValueError('Invalid recipe registration: ' + id)
    return Recipe(id, kind, language, toolchain, tuple(argv), tuple(env), timeout, OUTPUT_LIMIT, False, needs, detect)


def _npm(name, kind, timeout=120):
    return _recipe('npm-' + name, kind, 'javascript', 'npm', (NPM_CLI, '--ignore-scripts', 'run', name), NPM_ENV,
                   timeout, 'node_modules', _script_detector(name))


REGISTRY = {r.id: r for r in (
    _recipe('python-unittest', 'test', 'python', 'python', ('-I', '-m', 'unittest', 'discover', '-v'),
            timeout=30, detect=_detect_unittest),
    _recipe('python-pytest', 'test', 'python', 'python-pytest', ('-I', '-m', 'pytest', '-q', '-p', 'no:cacheprovider'),
            needs='python-env', detect=_detect_pytest),
    _recipe('node-test', 'test', 'javascript', 'node', ('--test',), timeout=30, needs='node_modules', detect=_detect_node),
    _npm('test', 'test'),
    _npm('build', 'build', 300),
    _npm('lint', 'lint'),
    _npm('typecheck', 'lint'),
    _recipe('go-test', 'test', 'go', 'go', ('test', '-v', './...'), GO_ENV, 180, detect=_detect_go_test),
    _recipe('go-build', 'build', 'go', 'go', ('build', './...'), GO_ENV, 300, detect=_detect_go),
    _recipe('cargo-test', 'test', 'rust', 'cargo', ('test', '--offline', '--locked'), CARGO_ENV, 300, detect=_detect_cargo),
    _recipe('cargo-build', 'build', 'rust', 'cargo', ('build', '--offline', '--locked'), CARGO_ENV, 300, detect=_detect_cargo),
    _recipe('make-test', 'test', 'make', 'make', ('-f', 'Makefile', 'test'), timeout=120, detect=_detect_make),
)}


# Bounded facts. Staged drafts overlay disk exactly as the test snapshot will.

def _manifest(ws, name, read=True):
    """(present, text, issue) for one root manifest; never follows links."""
    draft = ws.drafts.get(name)
    if draft is not None:
        try:
            data = draft['after'].encode('utf-8')
        except (AttributeError, KeyError, TypeError, UnicodeEncodeError):
            return True, None, f'{name} draft is not UTF-8 text.'
    else:
        try:
            target = ws.path(name)
        except FileNotFoundError:
            return False, None, None
        except (PolicyError, OSError):
            return True, None, f'{name} is not a regular project file.'
        if not read:
            return True, None, None
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
        except OSError:
            return True, None, f'{name} could not be read.'
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return True, None, f'{name} is not a regular project file.'
            if info.st_size > MANIFEST_LIMIT:
                return True, None, f'{name} exceeds the 256 KB manifest limit.'
            with os.fdopen(fd, 'rb') as source:
                fd = None
                data = source.read(MANIFEST_LIMIT + 1)
        finally:
            if fd is not None:
                os.close(fd)
    if len(data) > MANIFEST_LIMIT:
        return True, None, f'{name} exceeds the 256 KB manifest limit.'
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return True, None, f'{name} is not valid UTF-8.'
    if '\x00' in text:
        return True, None, f'{name} contains NUL bytes.'
    return True, text, None


def _unique(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate key')
    return dict(pairs)


def _package(present, text, issue):
    if not present:
        return None, None
    if issue:
        return None, issue
    try:
        data = json.loads(text, object_pairs_hook=_unique)
    except RecursionError:
        return None, 'package.json nests too deeply.'
    except ValueError:
        return None, 'package.json is not valid JSON or repeats a key.'
    if not isinstance(data, dict):
        return None, 'package.json must contain a JSON object.'
    scripts = data.get('scripts', {})
    if not isinstance(scripts, dict):
        return None, 'package.json scripts must be an object.'
    return {'scripts': scripts}, None


def facts(ws, inventory=True):
    texts, issues, present = {}, {}, set()
    for name in MANIFESTS:
        found, text, issue = _manifest(ws, name)
        if found:
            present.add(name)
            texts[name], issues[name] = text, issue
    if _manifest(ws, 'Cargo.lock', read=False)[0]:
        present.add('Cargo.lock')
    files, truncated = (), False
    if inventory:
        listed = ws.inventory()
        known = set(listed['files'])
        files = tuple(listed['files']) + tuple(sorted(n for n in ws.drafts if n not in known))
        truncated = listed['truncated']
    package, package_issue = _package('package.json' in present, texts.get('package.json'), issues.get('package.json'))
    return {'files': files, 'truncated': truncated, 'texts': texts, 'issues': issues, 'present': present,
            'package': package, 'package_issue': package_issue}


def _script(facts, name):
    if facts['package_issue']:
        raise PolicyError(facts['package_issue'])
    if facts['package'] is None:
        raise PolicyError('package.json is required for npm recipes.')
    script = facts['package']['scripts'].get(name)
    if script is None:
        raise PolicyError(f'package.json has no "{name}" script.')
    if not isinstance(script, str) or not script.strip():
        raise PolicyError(f'The package.json "{name}" script must be a non-empty string.')
    if len(script) > SCRIPT_LIMIT:
        raise PolicyError(f'The package.json "{name}" script exceeds the 4096 character review limit.')
    if any((ord(c) < 32 and c != '\t') or ord(c) == 127 for c in script):
        raise PolicyError(f'The package.json "{name}" script contains control characters.')
    return script


def _requirements(recipe, facts):
    """Manifest conditions a recipe needs; returns the literal npm script, if any."""
    def manifest(name, message):
        if facts['issues'].get(name):
            raise PolicyError(facts['issues'][name])
        if facts['texts'].get(name) is None:
            raise PolicyError(message)
        return facts['texts'][name]
    if recipe.toolchain == 'npm':
        return _script(facts, recipe.argv[-1])
    if recipe.toolchain == 'go' and not re.search(r'^module[ \t]+\S+', manifest('go.mod', 'go.mod is required for Go recipes.'), re.M):
        raise PolicyError('go.mod must declare a module.')
    if recipe.toolchain == 'cargo':
        manifest('Cargo.toml', 'Cargo.toml is required for Cargo recipes.')
        if 'Cargo.lock' not in facts['present']:
            raise PolicyError('Cargo.lock is required for an offline locked Cargo build.')
    if recipe.toolchain == 'make' and not MAKE_TEST.search(manifest('Makefile', 'A Makefile is required for make recipes.')):
        raise PolicyError('The Makefile has no literal test: target.')
    return None


# Toolchain location, confinement and identity.

def _unsafe(text):
    return any(ord(c) < 32 or ord(c) == 127 or c in '"\\' for c in text)


def _real(path):
    try:
        return Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise PolicyError('The toolchain executable is unavailable.') from None


def _confined(path, project):
    """A toolchain or environment read root: never /, home-wide or overlapping the project."""
    try:
        real = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise PolicyError('A toolchain folder is unavailable.') from None
    if not real.is_dir() or _unsafe(str(real)):
        raise PolicyError('A toolchain folder must be a plain directory path.')
    home = Path.home().resolve()
    if real == Path(real.anchor) or home.is_relative_to(real):
        raise PolicyError('Cannot establish a confined development runtime. Install a supported toolchain in its own folder.')
    if project is not None and (real.is_relative_to(project) or Path(project).is_relative_to(real)):
        raise PolicyError('A toolchain overlapping the selected project cannot run tests.')
    return real


def _trusted(info, executable=True):
    if not stat.S_ISREG(info.st_mode):
        raise PolicyError('The toolchain executable must be a regular file.')
    if info.st_size > EXECUTABLE_LIMIT:
        raise PolicyError('The toolchain executable exceeds the 512 MB identity limit.')
    if info.st_uid not in (0, os.getuid()):
        raise PolicyError('The toolchain executable must be owned by root or the current user.')
    if info.st_mode & stat.S_IWOTH:
        raise PolicyError('The toolchain executable must not be world-writable.')
    if info.st_mode & stat.S_IWGRP and not (info.st_uid == 0 and info.st_gid in TRUSTED_WRITER_GROUPS):
        raise PolicyError('The toolchain executable must not be group-writable.')
    if executable and not info.st_mode & stat.S_IXUSR:
        raise PolicyError('The toolchain executable is not executable.')


def _outside(real, project):
    if project is not None and (real == Path(project) or real.is_relative_to(project)):
        raise PolicyError('A toolchain inside the selected project cannot run tests.')


def pin(path, project=None, executable=True):
    """Identity of one toolchain file, hashed through a no-follow descriptor."""
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute() or _unsafe(str(path)):
        raise PolicyError('A pinned toolchain path must be a plain absolute path.')
    real = _real(path)
    _outside(real, project)
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        raise PolicyError('The toolchain executable is unavailable.') from None
    try:
        before = os.fstat(fd)
        _trusted(before, executable)
        hasher, total = hashlib.sha256(), 0
        while chunk := os.read(fd, 1 << 20):
            hasher.update(chunk)
            total += len(chunk)
            if total > EXECUTABLE_LIMIT:
                raise PolicyError('The toolchain executable exceeds the 512 MB identity limit.')
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if total != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise PolicyError('The toolchain executable changed while it was pinned.')
    return {'path': str(path), 'realpath': str(real), 'sha256': hasher.hexdigest(), 'size': before.st_size,
            'mtime_ns': before.st_mtime_ns, 'dev': before.st_dev, 'ino': before.st_ino, 'uid': before.st_uid,
            'mode': before.st_mode}


def _check(path, project, executable=True):
    real = _real(path)
    _outside(real, project)
    try:
        _trusted(os.stat(real), executable)
    except OSError:
        raise PolicyError('The toolchain executable is unavailable.') from None
    return real


def _toolchain(real):
    if real.is_relative_to(FRAMEWORK):
        return FRAMEWORK
    if real.is_relative_to(DEVELOPER) or real.parent in (Path('/usr/bin'), Path('/bin')):
        return DEVELOPER
    return real.parent.parent


def _has_pytest(prefix, real):
    version = re.fullmatch(r'python(\d+\.\d+)', real.name)
    if not version:
        return False
    site = Path(prefix) / 'lib' / f'python{version[1]}' / 'site-packages'
    return all((site / rel).is_file() for rel in ('pytest/__init__.py', '_pytest/__init__.py', 'pluggy/__init__.py'))


def _node():
    found = shutil.which('node')
    if found and Path(found).is_absolute():
        return Path(found)
    versions = []
    for candidate in (Path.home() / '.nvm/versions/node').glob('v*/bin/node'):
        version = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', candidate.parent.parent.name)
        if version:
            versions.append((tuple(map(int, version.groups())), str(candidate)))
    if not versions:
        raise PolicyError('Install Node to run this test recipe.')
    return Path(max(versions)[1])


def _cargo():
    for candidate in CARGOS:
        if candidate.exists():
            return candidate
    toolchains = sorted((Path.home() / '.rustup/toolchains').glob('*/bin/cargo'),
                        key=lambda p: (not p.parent.parent.name.startswith('stable-'), p.parent.parent.name))
    if not toolchains:
        raise PolicyError('Install Rust (cargo) to run Cargo recipes.')
    return toolchains[0]


def _locate(key, project, mount):
    env, entry, extra = {}, None, []
    if key == 'python':
        exe = next((p for p in PYTHONS if p.exists()), None)
        if exe is None:
            raise PolicyError('Install Python to run this test recipe.')
    elif key == 'python-pytest':
        if mount is not None:
            exe = Path(mount['executable']['path'])
            if not _has_pytest(exe.parent.parent, _real(exe)):
                raise PolicyError('The mounted Python environment does not provide pytest.')
        else:
            exe = next((p for p in PYTHONS if p.exists() and _has_pytest(_real(p).parent.parent, _real(p))), None)
            if exe is None:
                raise PolicyError('Install pytest for the framework Python, or mount a managed Python environment that provides it.')
    elif key in ('node', 'npm'):
        exe = _node()
        if key == 'npm':
            entry = _real(exe).parent.parent / 'lib/node_modules/npm/bin/npm-cli.js'
            if not entry.is_file():
                raise PolicyError('Install npm alongside Node to run npm recipes.')
    elif key == 'go':
        exe = next((p for p in GOS if p.exists()), None)
        if exe is None:
            raise PolicyError('Install Go to run Go recipes.')
        cache = Path.home() / 'go/pkg/mod'
        if cache.is_dir():
            extra.append(cache)
            env['GOMODCACHE'] = str(cache.resolve())
        else:
            env['GOMODCACHE'] = TEMP + '/go-mod'
    elif key == 'cargo':
        exe = _cargo()
        cargo_home = Path.home() / '.cargo'
        if cargo_home.is_dir():
            extra.append(cargo_home)
            env['CARGO_HOME'] = str(cargo_home.resolve())
        else:
            env['CARGO_HOME'] = TEMP + '/cargo-home'
        rustc = _real(exe).parent / 'rustc'
        if rustc.is_file():
            env['RUSTC'] = str(rustc)
    elif key == 'make':
        if not MAKE.exists():
            raise PolicyError('Install the Xcode Command Line Tools to run make recipes.')
        exe = MAKE
    else:
        raise PolicyError('Test recipe toolchain is not supported.')
    real = _check(exe, project)
    if entry is not None:
        entry = _check(entry, project, executable=False)
    root = _toolchain(real)
    if root == DEVELOPER:
        if not DEVELOPER.is_dir():
            raise PolicyError('Install the Xcode Command Line Tools to run this recipe.')
        env['DEVELOPER_DIR'] = str(DEVELOPER)
    reads = []
    for folder in [root, *extra]:
        folder = str(_confined(folder, project))
        if folder not in reads:
            reads.append(folder)
    return {'executable': exe, 'realpath': real, 'entrypoint': entry, 'reads': reads, 'env': env}


def _plain(value):
    if not isinstance(value, str) or not 0 < len(value) <= 1024 or not Path(value).is_absolute() or _unsafe(value):
        raise PolicyError('Environment mount paths must be plain absolute paths.')
    return Path(value)


def _identity_ok(value):
    return (isinstance(value, dict) and set(value) == set(IDENTITY)
            and all(isinstance(value[k], str) for k in ('path', 'realpath', 'sha256'))
            and re.fullmatch(r'[0-9a-f]{64}', value['sha256']) is not None
            and all(type(value[k]) is int for k in ('size', 'mtime_ns', 'dev', 'ino', 'uid', 'mode')))


def _mount(mount, recipe, project):
    """Normalized copy of a host-verified W4 mount spec, re-checked here."""
    if mount is None:
        return None
    if not isinstance(mount, dict) or not {'kind', 'root', 'reads'} <= set(mount) or set(mount) - MOUNT_KEYS:
        raise PolicyError('The environment mount must use exactly the managed mount fields.')
    kind = mount['kind']
    if kind not in MOUNT_KINDS or recipe.needs != kind:
        raise PolicyError('This recipe cannot use that managed environment.')
    root = _confined(_plain(mount['root']), project)
    if not isinstance(mount['reads'], list) or not 1 <= len(mount['reads']) <= 16:
        raise PolicyError('The environment mount needs one to sixteen read folders.')
    reads = []
    for folder in mount['reads']:
        folder = str(_confined(_plain(folder), project))
        if folder not in reads:
            reads.append(folder)
    if not any(root.is_relative_to(folder) for folder in reads):
        raise PolicyError('The environment mount must read its own root.')
    links = mount.get('links') or {}
    if not isinstance(links, dict) or set(links) - ({'node_modules'} if kind == 'node_modules' else set()):
        raise PolicyError('Environment links may only provide node_modules.')
    clean = {}
    for name, target in links.items():
        try:
            real = _plain(target).resolve(strict=True)
        except (OSError, RuntimeError):
            raise PolicyError('An environment link target is unavailable.') from None
        if not real.is_dir() or not real.is_relative_to(root):
            raise PolicyError('Environment links must stay inside the environment root.')
        clean[name] = str(real)
    if kind == 'node_modules' and 'node_modules' not in clean:
        raise PolicyError('A node_modules environment must link node_modules.')
    executable = mount.get('executable')
    if kind == 'python-env':
        # The interpreter is a venv link to its base Python; its folder, not its target, is inside the root.
        try:
            inside = _identity_ok(executable) and _plain(executable['path']).parent.resolve(strict=True).is_relative_to(root)
        except (OSError, RuntimeError):
            inside = False
        if not inside:
            raise PolicyError('A Python environment mount needs a pinned interpreter inside its root.')
        executable = dict(executable)
    elif executable is not None:
        raise PolicyError('Only a Python environment mount may supply an interpreter.')
    env = mount.get('env') or {}
    if not isinstance(env, dict) or len(env) > 8 or any(
            k not in MOUNT_ENV or not isinstance(v, str) or not 0 < len(v) <= 4096 or '\x00' in v for k, v in env.items()):
        raise PolicyError('Environment mounts may set only VIRTUAL_ENV and NODE_PATH.')
    labels = {}
    for key in ('environment_id', 'tree_digest'):
        value = mount.get(key)
        if value is not None and not (isinstance(value, str) and re.fullmatch(r'[0-9A-Za-z:_\-]{1,128}', value)):
            raise PolicyError('Environment mount labels must be short identifiers.')
        labels[key] = value
    return {'kind': kind, 'root': str(root), 'reads': reads, 'links': clean, 'executable': executable,
            'env': dict(sorted(env.items())), **labels}


def _build(recipe_id, ws, mount, project, facts_=None):
    """Every plan field except the pinned hashes, derived from fixed data."""
    recipe = REGISTRY.get(recipe_id) if isinstance(recipe_id, str) else None
    if recipe is None:
        raise PolicyError('Test recipe is not supported.')
    mount = _mount(mount, recipe, project)
    script = _requirements(recipe, facts_ or facts(ws, inventory=False))
    found = _locate(recipe.toolchain, project, mount)
    launch = found['executable'] if mount and mount['kind'] == 'python-env' else found['realpath']
    entry = found['entrypoint']
    argv = [str(launch)] + [str(entry) if part == NPM_CLI else part for part in recipe.argv]
    env = {'PATH': str(Path(launch).parent) + ':/usr/bin:/bin', 'HOME': TEMP, 'TMPDIR': TEMP,
           'PYTHONNOUSERSITE': '1', 'LANG': 'en_US.UTF-8', **dict(recipe.env), **found['env'],
           **(mount['env'] if mount else {})}
    return {'recipe': recipe.id, 'kind': recipe.kind, 'argv': argv, 'cwd': 'snapshot',
            'executable': {'path': str(found['executable']), 'realpath': str(found['realpath'])},
            'entrypoint': {'path': str(entry), 'realpath': str(entry)} if entry else None,
            'toolchain_reads': found['reads'], 'env': dict(sorted(env.items())), 'timeout': recipe.timeout,
            'output_limit': recipe.output_limit, 'network': False, 'script': script, 'mount': mount}


def _drift(current, expected, role):
    changed = [key for key in IDENTITY if current.get(key) != expected.get(key)]
    if changed:
        raise PolicyError(f'The pinned {role} changed after review ({", ".join(changed)}). Prepare the recipe again.')
    return current


def _auto(ws, mount):
    found = facts(ws)
    kind = mount.get('kind') if isinstance(mount, dict) else None
    for recipe_id in AUTO:
        recipe = REGISTRY[recipe_id]
        if mount is not None and recipe.needs != kind or not recipe.detect(found):
            continue
        try:
            _build(recipe_id, ws, mount, ws.root, found)
        except PolicyError:
            continue
        return recipe_id
    raise PolicyError('No available test recipe was detected in this project.')


def inspect(ws):
    """Detected recipes and their availability. Data only; grants nothing."""
    found = facts(ws)
    rows = []
    for recipe in REGISTRY.values():
        reason = recipe.detect(found)
        if not reason:
            continue
        row = {'id': recipe.id, 'kind': recipe.kind, 'reason': reason, 'available': True, 'issue': None}
        try:
            plan = _build(recipe.id, ws, None, ws.root, found)
            if plan['script'] is not None:
                row['script'] = plan['script']
        except PolicyError as exc:
            row.update(available=False, issue=str(exc))
        rows.append(row)
    if found['package_issue'] and not any(row['id'].startswith('npm-') for row in rows):
        rows.append({'id': 'npm-test', 'kind': 'test', 'reason': 'package.json', 'available': False,
                     'issue': found['package_issue']})
    return {'recipes': rows, 'authorization': 'none', 'inventory_truncated': found['truncated']}


def resolve(recipe_id, ws, mount=None):
    """Pinned plan for one registry recipe, or the deterministic 'auto' choice."""
    if recipe_id == 'auto':
        recipe_id = _auto(ws, mount)
    plan = _build(recipe_id, ws, mount, ws.root)
    derived = plan['executable']['realpath']
    if plan['mount'] and plan['mount']['kind'] == 'python-env':
        expected = plan['mount']['executable']
        plan['executable'] = _drift(pin(expected['path'], ws.root), expected, 'environment interpreter')
    else:
        plan['executable'] = pin(plan['executable']['path'], ws.root)
    if plan['entrypoint']:
        plan['entrypoint'] = pin(plan['entrypoint']['path'], ws.root, executable=False)
    if plan['executable']['realpath'] != derived:
        raise PolicyError('The toolchain executable changed while it was pinned.')
    return plan


def shape(plan):
    """Structural validation of a stored plan; returns its registry recipe."""
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS:
        raise PolicyError('The pinned test plan is malformed.')
    recipe = REGISTRY.get(plan['recipe']) if isinstance(plan['recipe'], str) else None
    argv, env = plan['argv'], plan['env']
    if not (recipe is not None and plan['kind'] == recipe.kind and plan['cwd'] == 'snapshot' and plan['network'] is False
            and type(plan['timeout']) is int and 0 < plan['timeout'] <= TIMEOUT_LIMIT
            and type(plan['output_limit']) is int and 0 < plan['output_limit'] <= OUTPUT_LIMIT
            and isinstance(argv, list) and 1 <= len(argv) <= 32 and all(isinstance(a, str) and '\x00' not in a for a in argv)
            and isinstance(env, dict) and len(env) <= 64
            and all(isinstance(k, str) and isinstance(v, str) and '\x00' not in k + v for k, v in env.items())
            and isinstance(plan['toolchain_reads'], list) and 1 <= len(plan['toolchain_reads']) <= 16
            and all(isinstance(p, str) for p in plan['toolchain_reads'])
            and (plan['script'] is None or isinstance(plan['script'], str))
            and (plan['mount'] is None or isinstance(plan['mount'], dict))
            and _identity_ok(plan['executable']) and (plan['entrypoint'] is None or _identity_ok(plan['entrypoint']))
            and argv[0] in (plan['executable']['realpath'], plan['executable']['path'])):
        raise PolicyError('The pinned test plan is malformed.')
    return recipe


def verify(plan, root=None):
    """Re-check every pinned identity immediately before launch."""
    shape(plan)
    checks = [('executable', plan['executable'], True)]
    if plan['entrypoint']:
        checks.append(('npm entry script', plan['entrypoint'], False))
    interpreter = (plan['mount'] or {}).get('executable')
    if interpreter and canonical(interpreter) != canonical(plan['executable']):
        checks.append(('environment interpreter', interpreter, True))
    for role, expected, executable in checks:
        _drift(pin(expected['path'], root, executable), expected, role)
    return plan


def matches(plan, ws, mount, project):
    """Re-derive the plan from the built snapshot; any difference is a refusal."""
    shape(plan)
    fresh = _build(plan['recipe'], ws, mount, project)
    for key in sorted(PLAN_KEYS - {'executable', 'entrypoint'}):
        if canonical(fresh[key]) != canonical(plan[key]):
            raise PolicyError(f'The test recipe changed after review ({key}). Propose the tests again.')
    for key in ('executable', 'entrypoint'):
        a, b = fresh[key], plan[key]
        if (a is None) != (b is None) or a and (a['path'], a['realpath']) != (b['path'], b['realpath']):
            raise PolicyError(f'The test recipe changed after review ({key}). Propose the tests again.')
    return fresh['mount']


# Learned procedures (memory_learning). A recipe teaches the procedure of its
# kind; a typecheck script is its own procedure. Guidance is bound to the bytes
# of the root manifests and lockfiles that decide what the recipe resolves or
# runs, so any change to them, including a file appearing or going, makes it
# stale. A binding is data: it grants nothing and selects nothing to run.

PROCEDURES = ('test', 'build', 'lint', 'typecheck')
TYPECHECK = frozenset({'npm-typecheck'})
BOUND = {'python': (), 'python-pytest': ('pyproject.toml', 'setup.cfg', 'pytest.ini', 'tox.ini', 'requirements.txt'),
         'node': ('package.json', 'package-lock.json'), 'npm': ('package.json', 'package-lock.json'),
         'go': ('go.mod', 'go.sum'), 'cargo': ('Cargo.toml', 'Cargo.lock'), 'make': ('Makefile',)}
# testing.FILE_LIMIT: a larger file is left out of the snapshot a recipe runs
# in, so a procedure cannot be bound to it.
BOUND_LIMIT = 2_000_000
SHA256 = re.compile(r'[0-9a-f]{64}')


class BindingBudget(PolicyError):
    """Reading the next bound file would exceed the caller's byte budget."""


def _registered(recipe_id):
    recipe = REGISTRY.get(recipe_id) if isinstance(recipe_id, str) else None
    if recipe is None:
        raise PolicyError('Test recipe is not supported.')
    return recipe


def procedure(recipe_id):
    """'test', 'build', 'lint' or 'typecheck' for one registry recipe."""
    recipe = _registered(recipe_id)
    return 'typecheck' if recipe.id in TYPECHECK else recipe.kind


def bound_files(recipe_id):
    """The root manifests and lockfiles a learned procedure of this recipe is bound to."""
    return BOUND[_registered(recipe_id).toolchain]


def _bound(ws, name, budget):
    """(SHA-256, or None when absent; bytes read) for one root file, its draft overlaid."""
    draft = ws.drafts.get(name)
    if draft is not None:
        try:
            data = draft['after'].encode('utf-8')
        except (AttributeError, KeyError, TypeError, UnicodeEncodeError):
            raise PolicyError(f'The {name} draft is not UTF-8 text.') from None
        if len(data) > budget:
            raise BindingBudget(f'Binding {name} would exceed the read budget.')
        return hashlib.sha256(data).hexdigest(), len(data)
    try:
        target = ws.path(name)
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return None, 0
    except OSError:
        raise PolicyError(f'{name} could not be read.') from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise PolicyError(f'{name} is not a regular project file.')
        if before.st_size > BOUND_LIMIT:
            raise PolicyError(f'{name} exceeds the 2 MB snapshot limit, so no procedure can be bound to it.')
        if before.st_size > budget:
            raise BindingBudget(f'Binding {name} would exceed the read budget.')
        hasher, total = hashlib.sha256(), 0
        while chunk := os.read(fd, 1 << 20):
            total += len(chunk)
            if total > min(BOUND_LIMIT, budget):
                raise PolicyError(f'{name} changed while it was read.')
            hasher.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if total != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise PolicyError(f'{name} changed while it was read.')
    return hasher.hexdigest(), total


def bindings(recipe_id, ws, budget=None):
    """({bound file: SHA-256 or None when absent}, bytes read) as ws holds the
    recipe's bound files, its staged drafts overlaid as the test snapshot will
    be. Refuses a link, a non-regular or unreadable file and a file above the
    snapshot limit; raises BindingBudget before reading past `budget` bytes."""
    names = bound_files(recipe_id)
    remaining = len(names) * BOUND_LIMIT if budget is None else budget
    found, read = {}, 0
    for name in names:
        found[name], used = _bound(ws, name, remaining - read)
        read += used
    return found, read


def review_bindings(recipe_id, ws):
    """{bound file: SHA-256, None when absent, or 'unbindable'} for the pre-launch check (X20 F20-03). Unlike
    bindings(), a file that cannot be bound (a link, a non-regular file or one above the snapshot limit) is marked
    instead of voiding the others, so the files that can be bound are still checked before launch."""
    found = {}
    for name in bound_files(recipe_id):
        try:
            found[name] = _bound(ws, name, BOUND_LIMIT)[0]
        except PolicyError:
            found[name] = 'unbindable'
    return found


def binding_digest(recipe_id, files):
    """Digest of one recipe's bound files: the identity a learned procedure keeps."""
    names = bound_files(recipe_id)
    if not isinstance(files, dict) or sorted(files) != sorted(names) or any(
            value is not None and not (isinstance(value, str) and SHA256.fullmatch(value)) for value in files.values()):
        raise PolicyError('The recorded recipe bindings are malformed.')
    return hashlib.sha256(canonical(['bound', recipe_id, [[name, files[name]] for name in names]]).encode()).hexdigest()


# Strict summary parsers. Each takes the last anchored framework summary; a
# test process still controls its own output, which is a documented limit.

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
UNITTEST = re.compile(r'^-{70}\nRan (\d+) tests? in \d+\.\d+s\n\n(OK|FAILED|NO TESTS RAN)(?: \(([^()\n]*)\))?[ \t]*$', re.M)
UNITTEST_ITEM = re.compile(r'(failures|errors|skipped|expected failures|unexpected successes)=(\d+)')
PYTEST_LINE = re.compile(r'^(?:=+ )?(?:no tests ran|\d+ [a-z]).* in \d+(?:\.\d+)?s\b.*$', re.M)
PYTEST_STRICT = re.compile(r'(?:=+ )?(no tests ran|\d+ [a-z]+(?: [a-z]+)?(?:, \d+ [a-z]+(?: [a-z]+)?)*) in \d+(?:\.\d+)?s(?: \(\d+:\d{2}:\d{2}\))?(?: =+)?')
PYTEST_WORDS = {'passed': 'passed', 'xpassed': 'passed', 'failed': 'failed', 'error': 'errors', 'errors': 'errors',
                'skipped': 'skipped', 'xfailed': 'skipped', 'deselected': None, 'warning': None, 'warnings': None,
                'rerun': None, 'subtests passed': None, 'subtests skipped': None, 'subtests xfailed': None,
                'subtests xpassed': None, 'subtests failed': 'failed'}
NODE = re.compile(r'^([#ℹ]) tests (\d+)\n\1 suites (\d+)\n\1 pass (\d+)\n\1 fail (\d+)\n\1 cancelled (\d+)\n'
                  r'\1 skipped (\d+)\n\1 todo (\d+)\n\1 duration_ms \d+(?:\.\d+)?$', re.M)
GO_TEST = re.compile(r'^--- (PASS|FAIL|SKIP): \S+ \(\d+(?:\.\d+)?s\)$', re.M)
GO_PACKAGE = re.compile(r'^(?:(?P<ok>ok  )\t\S+\t(?:\d+(?:\.\d+)?s|\(cached\))(?P<none> \[no tests to run\])?(?: coverage: [^\n]*)?'
                        r'|(?P<fail>FAIL)\t\S+\t\d+(?:\.\d+)?s|FAIL\t\S+ \[(?P<broken>build failed|setup failed)\]'
                        r'|\?   \t\S+\t(?P<empty>\[no test files\]))$', re.M)
CARGO = re.compile(r'^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; (\d+) measured; '
                   r'(\d+) filtered out(?:; finished in \d+(?:\.\d+)?s)?$', re.M)
JEST = re.compile(r'^Tests:\s+((?:\d+ (?:failed|skipped|todo|passed), )*)(\d+) total$', re.M)
JEST_SUITES = re.compile(r'^Test Suites:\s+((?:\d+ (?:failed|skipped|passed), )*)(\d+) total$', re.M)
MOCHA = re.compile(r'^ {2}(\d+) passing \(\d+(?:ms|s|m)\)$(?:\n {2}(\d+) pending$)?(?:\n {2}(\d+) failing$)?', re.M)
VITEST = re.compile(r'^ *Tests  (\d+ (?:failed|passed|skipped|todo)(?: \| \d+ (?:failed|passed|skipped|todo))*) \((\d+)\)$', re.M)


def _last(pattern, text):
    match = None
    for match in pattern.finditer(text):
        pass
    return match


def _tests(passed, failed, skipped, errors, total):
    return {'passed': passed, 'failed': failed, 'skipped': skipped, 'errors': errors, 'total': total}


def parse_unittest(text):
    match = _last(UNITTEST, text)
    if not match:
        return None
    total, status = int(match[1]), match[2]
    counts = dict.fromkeys(('failures', 'errors', 'skipped', 'expected failures', 'unexpected successes'), None)
    for item in (match[3].split(', ') if match[3] is not None else ()):
        part = UNITTEST_ITEM.fullmatch(item)
        if not part or counts[part[1]] is not None:
            return None
        counts[part[1]] = int(part[2])
    counts = {k: v or 0 for k, v in counts.items()}
    failed = counts['failures'] + counts['unexpected successes']
    errors, skipped = counts['errors'], counts['skipped'] + counts['expected failures']
    consistent = {'OK': not (failed or errors), 'FAILED': bool(failed or errors),
                  'NO TESTS RAN': not (total or failed or errors)}[status]
    if not consistent:
        return None
    return {'end': match.end(), 'tests': _tests(max(0, total - failed - errors - skipped), failed, skipped, errors, total)}


def parse_pytest(text):
    match = _last(PYTEST_LINE, text)
    strict = match and PYTEST_STRICT.fullmatch(match[0])
    if not strict:
        return None
    counts = dict.fromkeys(('passed', 'failed', 'skipped', 'errors'), 0)
    if strict[1] != 'no tests ran':
        seen = set()
        for item in strict[1].split(', '):
            number, word = item.split(' ', 1)
            if word not in PYTEST_WORDS or word in seen:
                return None
            seen.add(word)
            if PYTEST_WORDS[word]:
                counts[PYTEST_WORDS[word]] += int(number)
    total = sum(counts.values())
    return {'end': match.end(), 'tests': _tests(counts['passed'], counts['failed'], counts['skipped'], counts['errors'], total)}


def parse_node(text):
    match = _last(NODE, text)
    if not match:
        return None
    total, _, passed, failed, cancelled, skipped, todo = map(int, match.groups()[1:])
    return {'end': match.end(), 'tests': _tests(passed, failed, skipped + todo, cancelled, total)}


def parse_go(text):
    packages = list(GO_PACKAGE.finditer(text))
    if not packages:
        return None
    results = [m[1] for m in GO_TEST.finditer(text)]
    passed, failed, skipped = results.count('PASS'), results.count('FAIL'), results.count('SKIP')
    broken = sum(1 for m in packages if m['broken'])
    failing = sum(1 for m in packages if m['fail'])
    errors = broken + (failing if not failed else 0)
    return {'end': packages[-1].end(), 'tests': _tests(passed, failed, skipped, errors, passed + failed + skipped)}


def parse_cargo(text):
    lines = list(CARGO.finditer(text))
    if not lines:
        return None
    passed = failed = ignored = 0
    for line in lines:
        if (line[1] == 'ok') == (int(line[3]) > 0):
            return None
        passed, failed, ignored = passed + int(line[2]), failed + int(line[3]), ignored + int(line[4])
    return {'end': lines[-1].end(), 'tests': _tests(passed, failed, ignored, 0, passed + failed + ignored)}


def _items(text, separator):
    counts = {}
    for item in filter(None, (part.strip() for part in text.split(separator))):
        number, word = item.split(' ', 1)
        if word in counts:
            return None
        counts[word] = int(number)
    return counts


def parse_jest(text):
    match = _last(JEST, text)
    counts = match and _items(match[1], ',')
    if counts is None:
        return None
    suites = _last(JEST_SUITES, text)
    suite_counts = (suites and _items(suites[1], ',')) or {}
    total = int(match[2])
    errors = suite_counts.get('failed', 0) if not counts.get('failed') else 0
    return {'end': match.end(), 'tests': _tests(counts.get('passed', 0), counts.get('failed', 0),
                                                counts.get('skipped', 0) + counts.get('todo', 0), errors, total)}


def parse_mocha(text):
    match = _last(MOCHA, text)
    if not match:
        return None
    passed, pending, failed = int(match[1]), int(match[2] or 0), int(match[3] or 0)
    return {'end': match.end(), 'tests': _tests(passed, failed, pending, 0, passed + pending + failed)}


def parse_vitest(text):
    match = _last(VITEST, text)
    counts = match and _items(match[1], '|')
    if counts is None:
        return None
    return {'end': match.end(), 'tests': _tests(counts.get('passed', 0), counts.get('failed', 0),
                                                counts.get('skipped', 0) + counts.get('todo', 0), 0, int(match[2]))}


GENERIC = (parse_node, parse_unittest, parse_pytest, parse_jest, parse_mocha, parse_vitest, parse_go, parse_cargo)
PARSERS = {'python-unittest': (parse_unittest,), 'python-pytest': (parse_pytest,), 'node-test': (parse_node,),
           'go-test': (parse_go,), 'cargo-test': (parse_cargo,), 'npm-test': GENERIC, 'make-test': GENERIC}


def clean(text):
    return ANSI.sub('', text.replace('\r\n', '\n').replace('\r', '\n'))


def parse(recipe_id, text):
    """Counts from the summary that ends last in the output, or None."""
    text = clean(text)
    found = [result for parser in PARSERS.get(recipe_id, GENERIC) if (result := parser(text))]
    return max(found, key=lambda result: result['end'])['tests'] if found else None


def classify(kind, recipe_id, exit_code, text):
    """(outcome, tests). Only a clean exit with real passing counts is 'passed'."""
    if kind != 'test':
        return ('passed' if exit_code == 0 else 'failed'), None
    tests = parse(recipe_id, text)
    if recipe_id == 'python-pytest' and exit_code == 5:
        return 'no_tests', tests
    if tests is None:
        return 'error', None
    if tests['failed'] or tests['errors']:
        return 'failed', tests
    if tests['total'] == 0 or not tests['passed']:
        return 'no_tests', tests
    return ('passed' if exit_code == 0 else 'error'), tests
