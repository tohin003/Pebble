"""Pebble 1.0 performance benchmarks PB1-PB5 (Phase 24, t24-bench); PB6-PB10 name their existing measurers.

  run --report FILE [--pb 1,2,3,4,5] [--scratch DIR] [--quick]
        PB1 send -> first provider request at 50/400/1,500 files; PB2 host time per step (fresh, 300 and 1,000 prior
        edits; 32K and 128K; 40 Read steps); PB3 `state`, copy pass and `stop` at 150/500/1,000/1,500 chats over
        JSON-RPC; PB4 idle CPU of the backend at 1,000 chats over 40 s; PB5 RSS and mirrors over 40 runs in a 400-file
        project. --quick runs small sizes for a smoke check (never report numbers).

Synthetic data under a scratch folder (default: a new /private/tmp/pebble-perf-<id>/), a fake HOME for the backend,
an egress tripwire, and a scripted model: these are host costs on this machine, not model latency or quality.
The report records the machine, macOS version and power state. Nothing is overwritten.
"""

import argparse
import json
import platform
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT))

from benchmarks.perf import pb  # noqa: E402

SCHEMA = 'pebble.bench_perf/1'
FULL = {1: lambda root: pb.pb1(root), 2: lambda root: pb.pb2(root), 3: lambda root: pb.pb3(root),
        4: lambda root: pb.pb4(root), 5: lambda root: pb.pb5(root)}
QUICK = {1: lambda root: pb.pb1(root, sizes=(10, 60), samples=3),
         2: lambda root: pb.pb2(root, steps=4, prior_edits=(0, 30), budgets=(32768,)),
         3: lambda root: pb.pb3(root, chats=(5, 25), samples=3),
         4: lambda root: pb.pb4(root, chats=(25,), window=1.0, warmup=0.5),
         5: lambda root: pb.pb5(root, runs=4, files=40)}


def machine(power=True):
    info = {'platform': sys.platform, 'machine': platform.machine(), 'macos': platform.mac_ver()[0] or None,
            'python': platform.python_version()}
    if power:
        try:
            info['power'] = pb._script('measure_resources').power_source()
        except Exception as exc:  # a missing pmset never fails the benchmark
            info['power'] = 'unknown:' + type(exc).__name__
    return info


def run(benchmarks, scratch, *, quick=False, power=True):
    table = QUICK if quick else FULL
    root = Path(scratch)
    root.mkdir(mode=0o700)
    results = {}
    for number in benchmarks:
        started = datetime.now(timezone.utc).isoformat(timespec='seconds')
        results['PB%d' % number] = {'started_at': started, **table[number](root / ('pb%d' % number))}
    return {'schema': SCHEMA, 'quick': quick, 'machine': machine(power), 'results': results,
            'delegated': pb.DELEGATED, 'evidence': 'scripted host costs; synthetic data; no model'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    sub = commands.add_parser('run')
    sub.add_argument('--report', type=Path, required=True)
    sub.add_argument('--pb', default='1,2,3,4,5')
    sub.add_argument('--scratch', type=Path)
    sub.add_argument('--quick', action='store_true')
    args = parser.parse_args(argv)
    if args.report.exists() or args.report.is_symlink():
        print('Refused: the report path already exists.', file=sys.stderr)
        return 2
    numbers = [int(n) for n in args.pb.split(',')]
    if any(n not in FULL for n in numbers):
        print('Refused: choose benchmarks from 1 to 5 (PB6-PB10 use their own measurers).', file=sys.stderr)
        return 2
    scratch = args.scratch or Path('/private/tmp') / ('pebble-perf-' + secrets.token_hex(4))
    report = run(numbers, scratch, quick=args.quick)
    with args.report.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(json.dumps({k: v for k, v in report['results'].items()}, indent=2, sort_keys=True)[:4000])
    return 0


if __name__ == '__main__':
    sys.exit(main())
