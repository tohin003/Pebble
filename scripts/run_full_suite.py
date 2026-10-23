#!/usr/bin/env python3
"""Phase 32 (T1, T2): the full backend suite in parallel, with one proof log the release finalizers read.

  run_full_suite.py --out NEW_DIR [--workers N] [--python PY]

Two pytest sessions over backend/tests, then one merged log:
  - a: every test not marked `serial`, with pytest-xdist (`-n N --dist loadfile`, so a file's fixtures stay in one
    worker);
  - b: the tests marked `serial` (backend/tests/serial_register.json names each with its reason), one at a time, after
    session a, so nothing else runs beside them;
  - proof.log: both sessions' proof logs (backend/tests/proof_log.py) merged in the order of a plain collection of the
    whole suite, with that collection's count and one summary. Every collected test must have exactly one row, from
    exactly one session; a test that reported in neither is missing, and the log stays incomplete.
The serial-format rows are pytest's own outcomes; the merge changes only their order and the progress column.
Test scratch (each session's --basetemp) goes to a new folder under /private/tmp, never into the checkout or --out:
tests refuse scratch inside the checkout or outside /private/tmp (the first Phase 32 measurement put it under .runtime
and 56 tests failed and 27 errored on that alone). It is removed when every test passed and kept, with its path in
summary.json, when one did not.
Exit code: 0 when every test passed, 1 otherwise, 2 for bad arguments.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = 'backend/tests'
ROW = re.compile(r'^(backend/tests/[\w/.-]+\.py::\S.*?) (PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR) \[\s*\d+%\]\s*$')
COMMON = ['-p', 'no:cacheprovider', '-o', 'tmp_path_retention_policy=failed']
SCRATCH_PARENT = Path('/private/tmp')


def collect(python, root=ROOT):
    """The suite's node IDs in plain collection order (the order a serial run uses), relative to the checkout as the
    proof log writes them. backend/tests/pytest.ini makes backend/tests pytest's rootdir, so `--collect-only -q` lists
    `test_x.py::test` there; a suite without one lists `backend/tests/test_x.py::test`."""
    done = subprocess.run([python, '-m', 'pytest', TESTS, '--collect-only', '-q', *COMMON], cwd=root,
                          capture_output=True, text=True)
    ids = []
    for line in done.stdout.splitlines():
        line = line.strip()
        if '::' not in line or ' ' in line.split('::', 1)[0]:
            continue
        node = line if line.startswith(TESTS + '/') else TESTS + '/' + line
        if (root / node.split('::', 1)[0]).is_file():
            ids.append(node)
    if done.returncode != 0 or not ids:
        raise SystemExit('collection failed:\n' + done.stdout[-2000:] + done.stderr[-2000:])
    return ids


def rows(path):
    found = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        match = ROW.match(line)
        if match:
            if match.group(1) in found:
                raise SystemExit('a test has two rows in ' + str(path) + ': ' + match.group(1))
            found[match.group(1)] = match.group(2)
    return found


def session(python, out, name, extra, root=ROOT, scratch=None):
    log = out / (name + '.out')
    began = time.monotonic()
    with log.open('x') as stream:
        code = subprocess.run([python, '-m', 'pytest', TESTS, *COMMON, '--basetemp', str(scratch / ('cases-' + name)),
                               '--proof-log', str(out / (name + '.proof.log')), *extra],
                              cwd=root, stdout=stream, stderr=subprocess.STDOUT).returncode
    return code, round(time.monotonic() - began, 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--out', required=True, type=Path, help='a new folder for the logs and scratch')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--root', type=Path, default=ROOT, help='the checkout whose backend/tests run (tests use a fixture)')
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if out.exists():
        parser.error('--out already exists')
    if not 1 <= args.workers <= 16:
        parser.error('--workers must be 1-16')
    out.mkdir(mode=0o700, parents=True)
    began = time.monotonic()
    root = args.root.resolve()
    order = collect(args.python, root)
    scratch = Path(tempfile.mkdtemp(prefix='pebble-suite-', dir=SCRATCH_PARENT)).resolve()
    parallel = ['-n', str(args.workers), '--dist', 'loadfile', '-m', 'not serial']
    code_a, seconds_a = session(args.python, out, 'a', parallel, root, scratch)
    # pytest exits 5 when a marker selects nothing: an empty serial register is a valid state.
    code_b, seconds_b = session(args.python, out, 'b', ['-m', 'serial'], root, scratch)
    a, b = rows(out / 'a.proof.log'), rows(out / 'b.proof.log')
    both = sorted(set(a) & set(b))
    outcomes = {**a, **b}
    missing = [node for node in order if node not in outcomes]
    extra = sorted(set(outcomes) - set(order))
    counts = {}
    lines = ['# pebble proof log (scripts/run_full_suite.py): sessions a (parallel, '
             f'{args.workers} workers, not serial) and b (serial), merged in collection order',
             f'collected {len(order)} items']
    for index, node in enumerate(order, 1):
        status = outcomes.get(node)
        if status:
            counts[status] = counts.get(status, 0) + 1
            lines.append(f'{node} {status} [{index * 100 // len(order):3d}%]')
    lines += [f'{"ERROR" if outcomes[n] == "ERROR" else "FAILED"} {n}' for n in order if outcomes.get(n) in ('FAILED', 'ERROR')]
    words = [(counts.get('PASSED', 0), 'passed'), (counts.get('FAILED', 0), 'failed'), (counts.get('SKIPPED', 0), 'skipped'),
             (counts.get('XFAIL', 0), 'xfailed'), (counts.get('XPASS', 0), 'xpassed'), (counts.get('ERROR', 0), 'errors')]
    seconds = round(time.monotonic() - began, 2)
    lines.append(f'{"=" * 16} {", ".join(f"{n} {w}" for n, w in words if n or w == "passed")} in {seconds}s {"=" * 16}')
    with (out / 'proof.log').open('x', encoding='utf-8') as stream:
        stream.write('\n'.join(lines) + '\n')
    passed = (code_a == 0 and code_b in (0, 5) and not missing and not extra and not both
              and set(counts) <= {'PASSED'} and counts.get('PASSED') == len(order))
    summary = {'passed': passed, 'collected': len(order), 'outcomes': counts, 'missing': missing[:20],
               'extra': extra[:20], 'in_both_sessions': both[:20], 'session_a': {'exit': code_a, 'seconds': seconds_a},
               'session_b': {'exit': code_b, 'seconds': seconds_b, 'tests': len(b)}, 'seconds': seconds,
               'workers': args.workers, 'proof_log': str(out / 'proof.log')}
    if passed:
        shutil.rmtree(scratch, ignore_errors=True)
    else:
        summary['scratch_kept'] = str(scratch)
    (out / 'summary.json').write_text(json.dumps(summary, indent=1) + '\n')
    print(json.dumps(summary, indent=1))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
