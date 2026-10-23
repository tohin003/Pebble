#!/usr/bin/env python3
"""Phase 29 service validator: the Claude benchmark's program under test is pinned, and an absent CLI is never an answer.

Phase 28 proved the rebuilt 1.0.2 candidate (reports/harness-phase28-review.json). The Claude benchmark's first claim
plan then lost a whole Claude Code batch: another process rewrote the installed CLI mid-run, every cell of the batch
failed in two seconds with no session, and the driver recorded them as wrong answers (.runtime/
decisions-after-phase28.json D36). The harness now copies the CLI once per plan root under its SHA-256
(claude_cell.pin_binary) and runs every batch on that copy, and a turn with no init event and a failing exit is a
driver error (claude_unavailable) that stops the run (driver_stop). No product source changed. Checks, the same in
source and frozen modes (the benchmark harness is not part of the app, so frozen mode proves the same source):
  - the CLI is pinned once per plan and an update cannot reach it: the copy is made under its digest, reused, and keeps
    its bytes when the installed file is replaced; a missing CLI is refused;
  - a CLI that never starts is a driver error and stops the run: a scripted run whose CLI emits nothing records the
    first cell as error_driver claude_unavailable and the rest as not_run driver_stop, and the report names the pinned
    digest;
  - the release version is after 1.0.1 and every declaration agrees.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from validate_runtime import ROOT  # noqa: E402
from validate_phase13 import bounded, owned_backend, sha  # noqa: E402
from validate_phase20 import backend_path, write_new  # noqa: E402
from validate_phase21 import checkout_version, declared_versions  # noqa: E402

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'the_cli_is_pinned_once_per_plan_and_an_update_cannot_reach_it',
    'a_cli_that_never_starts_is_a_driver_error_and_stops_the_run',
    'release_version_is_after_1_0_1',
    'backend_inputs_stable_during_campaign',
)
# The tested sources this validator reads; each is in verify_harness_candidate.sources(), so the campaign binds them.
READS = ('benchmarks/drivers/claude_cell.py', 'scripts/bench_claude.py', 'benchmarks/plan_24a.py',
         'scripts/validate_phase29.py', 'backend/pebble/__init__.py', 'package.json', 'package-lock.json',
         'src-tauri/tauri.conf.json', 'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock')
MODEL = 'claude-opus-5-5'
BUDGET = {'usd_cap': 100, 'stop_at_usd': 90, 'prices_per_mtok': {'input': 5.0, 'cache_read': 0.2, 'output': 20.0},
          'prices_source': 'https://platform.claude.com/docs/en/about-claude/pricing', 'prices_retrieved': '2026-10-23'}


def bench():
    backend_path()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from benchmarks import plan_24a
    from benchmarks.drivers import claude_cell
    return plan_24a, claude_cell


def load_script(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


class Campaign:
    def __init__(self, scratch, backend=None):
        self.scratch, self.backend, self.results = scratch, backend, []

    def check(self, name, fn):
        assert name in PLANNED and name not in {r['name'] for r in self.results}, name
        began = time.monotonic()
        try:
            evidence = fn() or {}
            self.results.append({'name': name, 'passed': True, 'evidence': evidence,
                                 'seconds': round(time.monotonic() - began, 2)})
        except Exception as exc:
            self.results.append({'name': name, 'passed': False, 'error': bounded(exc, 1500),
                                 'trace': traceback.format_exc(limit=3)[-1200:].replace(str(ROOT), '<checkout>')})

    def run(self):
        scratch = self.scratch

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            return {'outside_checkout': True}
        self.check('fixture_scratch_created_outside_the_checkout', created)

        bp, cc = bench()

        def pinned():
            installed = scratch / 'installed' / 'claude.exe'
            installed.parent.mkdir()
            installed.write_bytes(b'cli build one')
            installed.chmod(0o755)
            root = scratch / 'plan-root'
            copy, digest = cc.pin_binary(installed, root)
            assert copy != installed and copy.read_bytes() == b'cli build one', copy
            assert copy.parent.parent.name == 'claude-cli' and copy.parent.name == digest[:16]
            assert cc.pin_binary(installed, root) == (copy, digest), 'a second batch made another copy'
            installed.unlink()
            installed.write_bytes(b'cli build two')
            assert copy.read_bytes() == b'cli build one' and cc.unchanged(copy, digest), 'the update reached the copy'
            try:
                cc.pin_binary(scratch / 'nowhere' / 'claude.exe', root)
                raise AssertionError('a missing CLI was pinned')
            except cc.ClaudeRefused as exc:
                assert 'claude_binary_missing' in str(exc)
            return {'digest_folder': True, 'reused': True, 'survives_replacement': True, 'missing': 'refused'}
        self.check('the_cli_is_pinned_once_per_plan_and_an_update_cannot_reach_it', pinned)

        def silent():
            cli = load_script('bench_claude_for_phase29', 'scripts/bench_claude.py')
            from pebble.evaluation import EvidenceLedger

            class Silent:
                def __init__(self, argv, cwd, env):
                    self.child = type('Child', (), {'returncode': 1, 'stderr': None, 'poll': lambda self: 1})()

                def lines(self, deadline):
                    return [], None

                def stop(self):
                    pass

            plan = bp.make_plan('claude', 'pilot', seed=29, model=MODEL, budget=BUDGET)
            ledger = EvidenceLedger(scratch / 'ledger')
            bp.register(plan, ledger)
            bp.stamp(plan, ledger, decision='D36', statement='Phase 29 fixture: a CLI that never starts.')
            tasks = ('c1-p75', 'f7-west', 'd5-minutes')
            positions = [c['position'] for c in plan['schedule'] if c['product'] == 'claude_code' and c['task'] in tasks]
            config = scratch / 'claude-config'
            config.mkdir(mode=0o700)
            cli._online = lambda: True
            parent = Path(tempfile.mkdtemp(prefix='pebble-p29-', dir='/private/tmp'))
            try:
                report = cli.run_claude_code(
                    plan, ledger=ledger, binary='/opt/claude', config_dir=config, parent=parent, positions=positions,
                    probe=lambda b: {'ok': True, 'version': '2.1.309', 'missing_flags': []},
                    signed=lambda b, c: {'signed_in': True, 'subscription': 'team'}, spawn=Silent,
                    check=lambda *a, **k: {'ok': True, 'isolation': [], 'failure': None, 'models': [MODEL],
                                           'helper_models': [], 'rate_limit': None, 'cost_usd_reported': 0.0},
                    pin=lambda b, root: (b, 'e' * 64))
            finally:
                shutil.rmtree(parent, ignore_errors=True)
            cells = [(c['outcome'], c['error']) for c in report['cells']]
            assert cells == [('error_driver', 'claude_unavailable'), ('not_run', 'driver_stop'),
                             ('not_run', 'driver_stop')], cells
            assert report['versions']['driver_stopped_cells'] == 2 and report['versions']['claude_code_sha256'] == 'e' * 64
            assert not any(c['outcome'] == 'wrong' for c in report['cells']), 'an absent CLI was scored as an answer'
            return {'cells': cells, 'pinned_digest_reported': True}
        self.check('a_cli_that_never_starts_is_a_driver_error_and_stops_the_run', silent)

        def version():
            current = checkout_version()
            parts = tuple(int(x) for x in current.split('.'))
            assert parts > (1, 0, 1), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_after_1_0_1', version)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary folder even on success')
    args = parser.parse_args(argv)
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; choose a new path (earlier raw evidence is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    backend_sha = sha(backend.read_bytes()) if backend else None
    before = digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase29-')).resolve()
    started = time.monotonic()
    campaign = Campaign(scratch, backend)
    fatal = None
    try:
        campaign.run()
    except Exception as exc:
        fatal = bounded(exc, 1500)
    after = digests()
    stable = before == after and (not backend or sha(backend.read_bytes()) == backend_sha)
    campaign.results.append({'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                             'evidence': {'files_compared': len(before), 'executable_compared': bool(backend)}})
    recorded = {r['name'] for r in campaign.results}
    failed = [r['name'] for r in campaign.results if not r['passed']] + [n for n in PLANNED if n not in recorded]
    passed = fatal is None and not failed and recorded == set(PLANNED)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source', 'passed': passed,
        'checks': [r['name'] for r in campaign.results if r['passed']], 'failed': failed, 'planned_checks': list(PLANNED),
        'check_results': campaign.results, 'fatal_error': fatal,
        'duration_seconds': round(time.monotonic() - started, 1),
        'backend_sha256': backend_sha, 'source_sha256': before,
        'source_note': 'Digests of the tested sources this validator reads; the campaign binds them to its snapshot.',
    }
    if passed and not args.keep_scratch:
        shutil.rmtree(scratch, ignore_errors=True)
    else:
        report['scratch_preserved'] = str(scratch)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_new(report_path, json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']), 'failed': failed,
                      'fatal_error': fatal}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
