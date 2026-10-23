#!/usr/bin/env python3
"""Phase 32 (H4): keep a benchmark plan's cell folders as evidence before anything clears /private/tmp.

  bench_archive.py PLAN.json --out NEW.tar.gz [--parent /private/tmp]

Every product's cells live under /private/tmp/pebble-bench-<plan sha256[:12]>/<product>/<task>-<rep>/: the workspace as
the product left it, Pebble's cell home (its database holds the messages and applied actions) and Codex's last
messages. The Claude plan's Pebble homes were cleared before their failures were read, so its Pebble causes stayed
inferences; plan 6f4dc958's folders survived and settled three harness defects. Run this after each batch and before
any scratch cleanup.

The archive is new (never overwritten), owner-only (0600) and written outside the scratch root; its SHA-256 and the
cell count per product are printed as JSON. Exit code 2 for bad arguments or a missing scratch root.
"""
import argparse
import hashlib
import json
import os
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.drivers import workspace as ws  # noqa: E402


def archive(plan_sha256, out, parent=ws.TMP):
    root = ws.plan_root(plan_sha256, parent)
    out = Path(out).resolve()
    if not root.is_dir():
        raise SystemExit('no scratch root: ' + str(root))
    if out.is_relative_to(root):
        raise SystemExit('--out must be outside the scratch root')
    cells = {product.name: sum(1 for cell in product.iterdir() if cell.is_dir())
             for product in sorted(root.iterdir()) if product.is_dir()}
    out.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as raw, tarfile.open(fileobj=raw, mode='w:gz') as tar:
        tar.add(root, arcname=root.name, recursive=True)  # symlinks are stored as links, never followed
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    return {'archive': str(out), 'sha256': digest, 'bytes': out.stat().st_size, 'scratch_root': str(root),
            'cells': cells}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('plan', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--parent', type=Path, default=ws.TMP)
    args = parser.parse_args(argv)
    if args.out.exists() or args.out.is_symlink():
        parser.error('--out already exists')
    plan = json.loads(args.plan.read_text())
    print(json.dumps(archive(plan['plan_sha256'], args.out, args.parent), indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
