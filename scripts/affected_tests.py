#!/usr/bin/env python3
"""Phase 32 (T5): the backend test files a change can affect, for everyday checks. Never a release proof.

  affected_tests.py [--since SNAPSHOT.json] [--files PATH ...] [--run] [--workers N] [--json]

What changed: --files, or every bound source file whose SHA-256 differs from --since (default: the newest
.runtime/phase*-tested-source.json), plus new files under the bound folders.
What a test file depends on, found statically and conservatively:
  - its imports, followed through backend/pebble, backend/vendor and benchmarks transitively (a test that imports
    pebble.app reaches nearly everything, so it is selected by nearly every backend change);
  - scripts and other files it names as repo-relative string literals ('scripts/validate_phase24.py', 'README.md',
    'src/App.vue', 'src-tauri/src/main.rs', ...), and what those scripts import in turn.
Always selected: the safety set (permissions, the sandbox, data upgrades, the vault, the publish gates) and every test
file that itself changed. A changed file no test reaches, or a change to the test setup (conftest.py, pytest.ini, the
plugins, the locks), selects the whole suite: when unsure, run everything.
--run runs the selection with pytest-xdist and prints its summary; the exit code is pytest's.
"""
import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'backend/tests'
# Phase 32 (validate_phase32): every name is a real test file; the first draft named test_permissions.py and
# test_vault.py, which do not exist, so permissions and the vault were silently missing from the set.
SAFETY = ('test_agent_effects.py', 'test_reconcile.py', 'test_sandbox_profiles.py', 'test_migrations.py',
          'test_vault_consolidated_22.py', 'test_vault_fallback_24.py', 'test_publish_tree_24.py',
          'test_phase16_security.py', 'test_tool_authz.py', 'test_data_boundary_24.py')
EVERYTHING = {'backend/tests/conftest.py', 'backend/tests/pytest.ini', 'backend/tests/proof_log.py',
              'backend/tests/serial_register.py', 'backend/tests/serial_register.json', 'backend/requirements.txt',
              'backend/requirements.lock', 'backend/requirements-dev.txt'}
PATHLIKE = re.compile(r'^(?:backend|scripts|src|src-tauri|benchmarks|browser-extension|docs|publish|build|config|examples)/'
                      r'[\w./ -]+$|^(?:README\.md|CHANGELOG\.md|install\.sh|package\.json|package-lock\.json)$')
ROOTS = {'pebble': ROOT / 'backend/pebble', 'agentguard': ROOT / 'backend/vendor/agentguard', 'benchmarks': ROOT / 'benchmarks'}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def module_file(name):
    """The repo file of a dotted module name, or None outside the repo's own packages."""
    head, *rest = name.split('.')
    base = ROOTS.get(head)
    if base is None:
        candidate = ROOT / 'scripts' / (head + '.py')  # scripts import each other by bare name
        return candidate if candidate.is_file() and not rest else None
    path = base.joinpath(*rest)
    for option in (path.with_suffix('.py'), path / '__init__.py'):
        if option.is_file():
            return option
    return base / '__init__.py' if not rest and (base / '__init__.py').is_file() else None


def direct(path, cache):
    """Repo files one Python file depends on directly: imports and repo-relative path literals."""
    if path in cache:
        return cache[path]
    found = set()
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError, UnicodeDecodeError, OSError):
        cache[path] = found
        return found
    package = None
    if ROOT / 'backend/pebble' in path.parents:
        package = 'pebble'
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level and package:
                base = 'pebble'
            else:
                base = node.module or ''
            names = [base] + [f'{base}.{a.name}' for a in node.names] if base else []
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and PATHLIKE.match(node.value):
            target = ROOT / node.value
            if target.is_file():
                found.add(target)
        for name in names:
            target = module_file(name)
            if target:
                found.add(target)
    found.discard(path)
    cache[path] = found
    return found


def closure(path, cache, memo):
    if path in memo:
        return memo[path]
    memo[path] = seen = set()
    stack = [path]
    while stack:
        for dep in direct(stack.pop(), cache):
            if dep not in seen:
                seen.add(dep)
                if dep.suffix == '.py':
                    stack.append(dep)
    return seen


def changed_files(since):
    snapshot = json.loads(Path(since).read_text())
    changed = {rel for rel, digest in snapshot.items() if not (ROOT / rel).is_file() or sha(ROOT / rel) != digest}
    for folder, suffixes in (('backend', {'.py'}), ('scripts', {'.py'}), ('src', {'.vue', '.ts', '.mjs', '.css'}),
                             ('benchmarks', {'.py'})):
        for path in (ROOT / folder).rglob('*'):
            rel = path.relative_to(ROOT).as_posix()
            if path.is_file() and path.suffix in suffixes and rel not in snapshot and '__pycache__' not in rel:
                changed.add(rel)
    return sorted(changed)


def select(changed):
    cache, memo = {}, {}
    tests = sorted(TESTS.glob('test_*.py'))
    reaches = {t: closure(t, cache, memo) for t in tests}
    chosen, reasons, unmapped = set(), {}, []
    for rel in changed:
        path = ROOT / rel
        if rel in EVERYTHING:
            return [t.name for t in tests], {'everything': rel}, []
        if path.parent == TESTS and path.name.startswith('test_'):
            chosen.add(path.name)
            reasons.setdefault(path.name, 'changed itself')
            continue
        hits = [t.name for t in tests if path in reaches[t]]
        if not hits:
            unmapped.append(rel)
        for name in hits:
            chosen.add(name)
            reasons.setdefault(name, 'reaches ' + rel)
    if unmapped:
        return [t.name for t in tests], {'everything': 'no test reaches ' + ', '.join(unmapped[:5])}, unmapped
    for name in SAFETY:
        if (TESTS / name).is_file():
            chosen.add(name)
            reasons.setdefault(name, 'safety set')
    return sorted(chosen), reasons, []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--since', type=Path)
    parser.add_argument('--files', nargs='*')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.files is not None:
        changed = sorted({Path(f).resolve().relative_to(ROOT).as_posix() for f in args.files})
    else:
        since = args.since or max(ROOT.glob('.runtime/phase*-tested-source.json'), key=lambda p: p.stat().st_mtime)
        changed = changed_files(since)
    chosen, reasons, unmapped = select(changed)
    report = {'changed': changed, 'test_files': len(chosen), 'of': len(list(TESTS.glob('test_*.py'))),
              'everything': reasons.get('everything'), 'unmapped': unmapped,
              'selected': chosen if args.json else None}
    print(json.dumps({k: v for k, v in report.items() if v is not None}, indent=1))
    if not args.run or not chosen:
        return 0
    command = [sys.executable, '-m', 'pytest', *[f'backend/tests/{name}' for name in chosen], '-q', '-p', 'no:cacheprovider',
               '-n', str(args.workers), '--dist', 'loadfile']
    return subprocess.run(command, cwd=ROOT).returncode


if __name__ == '__main__':
    sys.exit(main())
