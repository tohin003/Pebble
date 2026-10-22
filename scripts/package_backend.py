"""The frozen backend's Python inputs: the hashed lock and the build environment made from it (V1-076).

  package_backend.py lock-check                  offline: the lock pins every backend/requirements.txt pin at the
                                                 same version, and lists how many pins still lack a hash
  package_backend.py lock-hashes [--write]       network (parent): downloads each pinned wheel for this Mac with
                                                 pip, without dependencies, and records its SHA-256 in the lock
  package_backend.py build-env [--venv DIR]      network or pip's cache (parent): a new virtual environment at
                                                 DIR (default .runtime/build-env) installed from the lock alone,
                                                 with --require-hashes --no-deps --only-binary :all:

The candidate app is built only by scripts/build_candidate_app.py, from build/pebble-backend.spec and the
environment this tool makes; it refuses an environment that differs from the lock. This tool no longer writes
src-tauri/resources (Phase 20 retired that path). PYINSTALLER_FLAGS states the spec's collection as command-line
flags, for reviewers and for test_phase18_security's packaging check; a test keeps the two in agreement.

Exit codes: 0 done, 10 the lock is incomplete (lock-check), 2 refused or usage, 1 crash.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'backend/requirements.lock'
REQUIREMENTS = ROOT / 'backend/requirements.txt'
BUILD_ENV = ROOT / '.runtime/build-env'
SPEC = ROOT / 'build/pebble-backend.spec'
PIN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([A-Za-z0-9._+!-]+)$')
HASH = re.compile(r'--hash=sha256:([0-9a-f]{64})')
SECTION = re.compile(r'^#\s*\[(runtime|build)\]')
# build/pebble-backend.spec as flags. Excluded AgentGuard subpackages and libraries never ship (V1-076, V1-143,
# V1-045); pillow_heif is not collected.
PYINSTALLER_FLAGS = [
    '--noconfirm', '--onedir', '--name', 'pebble-backend',
    '--paths', 'backend/vendor',
    '--collect-submodules', 'agentguard',
    '--exclude-module', 'agentguard.daemon.app', '--exclude-module', 'agentguard.daemon.__main__',
    '--exclude-module', 'agentguard.cli', '--exclude-module', 'agentguard.adapters',
    '--exclude-module', 'agentguard.bench', '--exclude-module', 'agentguard.census',
    '--collect-all', 'tzdata',
    '--collect-submodules', 'jsonschema',
    '--hidden-import', 'tiktoken_ext.openai_public',
    '--collect-submodules', 'PIL',
    *[flag for language in ['javascript', 'typescript', 'tsx', 'go', 'rust', 'java', 'ruby']
      for flag in ['--hidden-import', 'tree_sitter_language_pack.bindings.' + language]],
    *[flag for module in ['fastapi', 'starlette', 'uvicorn', 'httpx', 'httpcore', 'anyio', 'h11', 'typer', 'click',
                          'rich', 'pygments', 'markdown_it', 'pytest', '_pytest', 'pluggy', 'ruff', 'opentelemetry',
                          'pillow_heif', '_pillow_heif', 'jsonschema.benchmarks',
                          # Phase 26: setuptools came in through urllib3's optional backports import
                          'setuptools', 'pkg_resources', '_distutils_hack', 'distutils']
      for flag in ['--exclude-module', module]],
    '--copy-metadata', 'certifi',  # Phase 24 (parent, trial 2): the bundled CA store's version is checkable
    'backend/main.py',
]


class Refused(Exception):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


def normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def read_lock(text):
    """[{name, version, section, hashes}] from a pip requirements lock: one 'name==version' per requirement, its
    '--hash=sha256:' options on the same logical line (backslash continuations), grouped by '# [runtime]' and
    '# [build]' comments. Anything else is refused rather than guessed."""
    pins, section, logical = [], None, ''
    for raw in text.splitlines() + ['']:
        line = raw.strip()
        found = SECTION.match(line)
        if found:
            section = found.group(1)
            continue
        if line.startswith('#'):
            continue
        logical += ' ' + line.rstrip('\\').strip()
        if line.endswith('\\'):
            continue
        parts = logical.split()
        logical = ''
        if not parts:
            continue
        match = PIN.match(parts[0])
        options = parts[1:]
        if not match or section is None or any(not HASH.fullmatch(option) for option in options):
            raise Refused('lock_line_unreadable', parts[0][:120])
        pins.append({'name': match.group(1), 'version': match.group(2), 'section': section,
                     'hashes': [HASH.fullmatch(option).group(1) for option in options]})
    seen = {}
    for pin in pins:
        key = normalize(pin['name'])
        if key in seen:
            raise Refused('lock_duplicate', pin['name'])
        seen[key] = pin
    return pins


def requirement_pins(text):
    pins = {}
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if line:
            match = PIN.match(line)
            if not match:
                raise Refused('requirement_unreadable', line[:120])
            pins[normalize(match.group(1))] = match.group(2)
    return pins


def lock_check(lock=LOCK, requirements=REQUIREMENTS):
    """Offline facts: missing or differing top-level pins, and pins without a hash."""
    pins = read_lock(Path(lock).read_text())
    by_name = {normalize(pin['name']): pin for pin in pins}
    differences = []
    for name, version in sorted(requirement_pins(Path(requirements).read_text()).items()):
        pin = by_name.get(name)
        if pin is None or pin['section'] != 'runtime':
            differences.append({'name': name, 'requirement': version, 'lock': None})
        elif pin['version'] != version:
            differences.append({'name': name, 'requirement': version, 'lock': pin['version']})
    unhashed = sorted(pin['name'] for pin in pins if not pin['hashes'])
    return {'pins': len(pins), 'runtime': sum(pin['section'] == 'runtime' for pin in pins),
            'build': sum(pin['section'] == 'build' for pin in pins), 'differences': differences,
            'unhashed': unhashed, 'complete': not differences and not unhashed}


def render_lock(text, hashes):
    """The lock text with each pin's hashes replaced by hashes[normalized name]; comments are kept."""
    out, section = [], None
    for pin_or_line in _lines(text):
        if isinstance(pin_or_line, str):
            out.append(pin_or_line)
            continue
        found = sorted(set(hashes.get(normalize(pin_or_line['name']), pin_or_line['hashes'])))
        head = pin_or_line['name'] + '==' + pin_or_line['version']
        out.append(head + ''.join(' \\\n    --hash=sha256:' + digest for digest in found))
    return '\n'.join(out) + '\n'


def _lines(text):
    """Comment and blank lines as text, each requirement as its parsed pin, in order."""
    logical, items = '', []
    for raw in text.splitlines():
        line = raw.strip()
        if not logical and (not line or line.startswith('#')):
            items.append(raw)
            continue
        logical += ' ' + line.rstrip('\\').strip()
        if line.endswith('\\'):
            continue
        parts, logical = logical.split(), ''
        match = PIN.match(parts[0])
        if not match:
            raise Refused('lock_line_unreadable', parts[0][:120])
        items.append({'name': match.group(1), 'version': match.group(2),
                      'hashes': [HASH.fullmatch(option).group(1) for option in parts[1:] if HASH.fullmatch(option)]})
    return items


def run(argv):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr)[-4000:]


def sha_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def lock_hashes(lock=LOCK, python=sys.executable, runner=run, write=False):
    """pip download each pin (this platform's wheel, no dependencies) into a new temporary folder and hash it."""
    text = Path(lock).read_text()
    pins = read_lock(text)
    hashes, problems = {}, []
    with tempfile.TemporaryDirectory(prefix='pebble-lock-') as folder:
        for pin in pins:
            target = Path(folder) / normalize(pin['name'])
            target.mkdir()
            code, output = runner([str(python), '-m', 'pip', 'download', '--no-deps', '--only-binary=:all:',
                                   '--disable-pip-version-check', '--dest', str(target),
                                   pin['name'] + '==' + pin['version']])
            files = sorted(target.iterdir())
            if code or len(files) != 1:
                problems.append({'name': pin['name'], 'exit_code': code, 'files': len(files), 'output': output[-400:]})
                continue
            hashes[normalize(pin['name'])] = [sha_file(files[0])]
    if problems:
        raise Refused('download_failed', problems)
    rendered = render_lock(text, hashes)
    if write:
        Path(lock).write_text(rendered)
    return {'pins': len(pins), 'hashed': len(hashes), 'written': bool(write)}


def build_env(venv=BUILD_ENV, lock=LOCK, python=sys.executable, runner=run):
    """A new venv installed from the hashed lock alone; an existing folder is refused, never replaced."""
    facts = lock_check(lock)
    if facts['unhashed']:
        raise Refused('lock_unhashed', facts['unhashed'])
    venv = Path(os.path.abspath(venv))
    if os.path.lexists(venv):
        raise Refused('venv_exists', str(venv))
    steps = [[str(python), '-m', 'venv', str(venv)],
             [str(venv / 'bin/python'), '-m', 'pip', 'install', '--disable-pip-version-check', '--require-hashes',
              '--no-deps', '--only-binary=:all:', '-r', str(lock)]]
    for argv in steps:
        code, output = runner(argv)
        if code:
            raise Refused('command_failed', {'argv': argv, 'exit_code': code, 'output': output[-400:]})
    return {'venv': str(venv), 'python': str(venv / 'bin/python'), 'pins': facts['pins']}


def main(argv=None, runner=run):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command')
    check = commands.add_parser('lock-check')
    check.add_argument('--lock', default=str(LOCK))
    check.add_argument('--requirements', default=str(REQUIREMENTS))
    hashing = commands.add_parser('lock-hashes')
    hashing.add_argument('--lock', default=str(LOCK))
    hashing.add_argument('--write', action='store_true', help='Rewrite the lock with the hashes (default: report only).')
    env = commands.add_parser('build-env')
    env.add_argument('--venv', default=str(BUILD_ENV))
    env.add_argument('--lock', default=str(LOCK))
    args = parser.parse_args(argv)
    try:
        if args.command == 'lock-check':
            result = lock_check(args.lock, args.requirements)
            print(json.dumps(result))
            return 0 if result['complete'] else 10
        if args.command == 'lock-hashes':
            result = lock_hashes(args.lock, runner=runner, write=args.write)
        elif args.command == 'build-env':
            result = build_env(args.venv, args.lock, runner=runner)
        else:
            raise Refused('retired', 'This tool no longer writes src-tauri/resources; build the app with '
                                     'scripts/build_candidate_app.py (see docs/HARNESS_CANDIDATE_APP.md).')
    except Refused as error:
        print(json.dumps({'refused': error.code, 'detail': error.detail}), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
