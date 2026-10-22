"""MR-1 synthetic memory recall evaluation (v1 versus hybrid v2), no model.

Builds a seeded corpus in a private in-memory database, recalls 36
paraphrased queries at 30, 300 and 1,500 noise records through both rankers
and writes one JSON report to a new --out path. Nothing else is read or
written: no model, network, profile, vault or app data directory is used.
Exits nonzero when v2 recalls a harmful record or omits a required conflict
note; every failure stays in the report either way.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pebble import memory_eval


def integers(text):
    try:
        values = tuple(int(part) for part in text.split(','))
    except ValueError:
        raise argparse.ArgumentTypeError('Use comma-separated integers.') from None
    if not values:
        raise argparse.ArgumentTypeError('Use at least one value.')
    return values


def write_new(path, report):
    """Create `path` exclusively; an existing file or link is never replaced."""
    data = (json.dumps(report, indent=2, sort_keys=True) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', type=Path, required=True, help='new JSON report path (must not exist)')
    parser.add_argument('--seed', type=int, default=15)
    parser.add_argument('--noise', type=integers, default=memory_eval.NOISE_LEVELS, help='e.g. 30,300,1500')
    parser.add_argument('--windows', type=integers, default=memory_eval.WINDOWS, help='model windows in tokens')
    parser.add_argument('--trigram', choices=('auto', 'python', 'fts5'), default='auto')
    args = parser.parse_args(argv)
    out = args.out
    if out.exists() or out.is_symlink():
        parser.error(f'{out} already exists; choose a new report path.')
    if not out.parent.is_dir():
        parser.error(f'{out.parent} is not an existing directory.')
    report = memory_eval.evaluate(args.seed, args.noise, args.windows, trigram=args.trigram)
    write_new(out, report)
    for row in report['results']:
        print(f"{row['ranker']} noise={row['noise']} window={row['window']} budget={row['budget']}: "
              f"recall={row['recall']} precision={row['precision']} mrr={row['mrr']} "
              f"harmful={row['harmful_inclusions']} p95={row['latency_ms']['p95']}ms")
    print(f"failures={len(report['failures'])} gates={report['gates']} sha256={report['deterministic_sha256']}")
    return 0 if all(report['gates'].values()) else 1


if __name__ == '__main__':
    sys.exit(main())
