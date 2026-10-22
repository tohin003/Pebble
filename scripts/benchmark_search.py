"""Repeatable generated-repository cache measurements, not a frontier benchmark."""

import argparse
import json
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pebble.search_index import SearchIndex
from pebble.policy import Workspace
from pebble.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='pebble-search-bench-') as directory:
        root = Path(directory) / 'project'
        root.mkdir()
        for i in range(2000):
            folder = root / f'group-{i // 100:02d}'
            folder.mkdir(exist_ok=True)
            (folder / f'file-{i:04d}.py').write_text(f'def fixture_{i}():\n    return {i}\n' + ('# rare-target\n' if i == 1999 else ''))
        store = Store(Path(directory) / 'data')
        index, ws = SearchIndex(store), Workspace(root)
        try:
            def query():
                started = time.perf_counter()
                result = index.grep(ws, 'rare-target', scope=('benchmark', 'run'))
                elapsed = (time.perf_counter() - started) * 1000
                assert result['coverage']['complete'] and result['untrusted_matches'][0]['path'].endswith('1999.py')
                return elapsed, result['coverage']['cache']['read']
            cold_ms, cold_reads = query()
            time.sleep(2.1)  # Explicit coarse-timestamp safety revalidation interval.
            revalidation_ms, revalidation_reads = query()
            samples = [query() for _ in range(7)]
            assert all(reads == 0 for _ms, reads in samples)
            target = root / 'group-19/file-1999.py'
            target.write_text(target.read_text().replace('rare-target', 'new-target'))
            stale = index.grep(ws, 'rare-target', scope=('benchmark', 'run'))
            assert not stale['untrusted_matches'] and stale['coverage']['complete']
            new = index.grep(ws, 'new-target', scope=('benchmark', 'run'))
            assert len(new['untrusted_matches']) == 1
            values = sorted(ms for ms, _reads in samples)
            report = {'date': datetime.now(timezone.utc).isoformat(), 'passed': True, 'fixture_files': 2000,
                'cold_ms': round(cold_ms, 3), 'cold_body_reads': cold_reads,
                'safety_revalidation_ms': round(revalidation_ms, 3), 'safety_revalidation_body_reads': revalidation_reads,
                'warm_samples': 7, 'warm_p50_ms': round(statistics.median(values), 3),
                'warm_p95_ms': round(values[-1], 3), 'warm_body_reads': [reads for _ms, reads in samples],
                'changed_file_invalidated': True,
                'limits': 'One generated local repository, seven warm observations; still walks metadata and scans cached blobs. No cold-start distribution, paid model, symbol index or competitive speed claim.'}
        finally:
            store.close()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
