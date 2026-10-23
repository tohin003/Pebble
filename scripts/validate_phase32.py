#!/usr/bin/env python3
"""Phase 32 service validator: faster tests that prove the same thing, and a benchmark harness that is fair.

The person asked for test runs that do not repeat everything every time ("we have to re think on how big companies
handle this situations", .runtime/phase32-plan.md, approved with the Stealth plan), and the beat-the-frontier plan
(.runtime/beat-plan/plan.md, D54) requires the benchmark harness defects found by the gpt-6.1-sol claim plan to be
fixed for every product before any rerun. These checks drive the real plugins and scripts on fixtures in a scratch
folder outside the checkout, the same in source and frozen modes (nothing here runs the backend):
  - a parallel run's proof log (backend/tests/proof_log.py) equals a serial run's rows, and the finalizers' row rule
    and release_readiness read it;
  - scripts/run_full_suite.py merges its parallel and serial sessions into one proof log in collection order, and a
    failure in either session fails the run;
  - backend/tests/serial_register.json is true: every entry has a reason and names collected tests, and a stale or
    unexplained entry stops a run;
  - scripts/affected_tests.py selects the tests a change reaches plus the safety set, and everything when a changed
    file reaches no test or is part of the test setup;
  - verify_harness_candidate.py with --jobs gives the same reports as a serial campaign and runs SERIAL_SUITES alone;
  - the benchmark harness defects (H1-H3) stay fixed, with D50's usage-window rules and the Codex resume flags.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import subprocess
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
from validate_phase20 import write_new  # noqa: E402

# In-process imports: benchmarks.drivers and pebble.release_readiness (the finalizers' log parser). Other validators run
# the backend as a process; this one reads the checkout's own modules, so both folders go on the path.
for _path in (ROOT / 'backend', ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'a_parallel_proof_log_equals_the_serial_one',
    'the_full_suite_runner_merges_both_sessions_and_fails_on_any_failure',
    'the_serial_register_is_true',
    'affected_tests_run_everything_when_unsure',
    'the_pooled_campaign_gives_the_same_reports',
    'benchmark_harness_defects_stay_fixed',
    'backend_inputs_stable_during_campaign',
)
# The tested sources this validator reads or runs; each is in verify_harness_candidate.sources(), so the campaign
# binds them.
READS = ('scripts/validate_phase32.py', 'scripts/run_full_suite.py', 'scripts/affected_tests.py',
         'scripts/verify_harness_candidate.py', 'backend/tests/proof_log.py', 'backend/tests/serial_register.py',
         'backend/tests/serial_register.json', 'backend/tests/conftest.py', 'backend/tests/pytest.ini',
         'backend/requirements-dev.txt', 'benchmarks/drivers/workspace.py', 'benchmarks/drivers/codex_cell.py',
         'benchmarks/drivers/claude_cell.py', 'benchmarks/drivers/chatgpt_limits.py', 'scripts/bench_codex.py',
         'scripts/bench_archive.py', 'backend/pebble/release_readiness.py')
TESTS = ROOT / 'backend' / 'tests'
# The finalizers' row rule (finalize_harness_phase*.py are not published; the same pattern as theirs).
FINALIZER_ROW = re.compile(r'^(backend/tests/[\w/.-]+\.py)::(\S.*?) (PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)'
                           r'(?: \(.*\))?\s+\[\s*\d+%\]\s*$')
BODY = '''
import time, pytest
@pytest.mark.parametrize("n", range(6))
def test_value(n):
    time.sleep(0.05)
    assert n >= 0
def test_alone():
    assert True
@pytest.mark.skip(reason="fixture skip")
def test_skipped():
    pass
@pytest.mark.xfail(reason="fixture xfail", strict=True)
def test_expected_failure():
    assert False
'''
# A second file, quick: under -n 2 --dist loadfile it runs on the other worker and reports before the first file's
# slower cases, so a log written in completion order differs from the serial one.
QUICK = 'def test_quick():\n    assert True\n'
# Codex's own replies in plan 6f4dc958 (kept cell folders): asked, and ended with the prompt's `FINAL: done`.
SOL_QUESTIONS = ('Which report should I copy to `summary.md`: `report_q3.md` or `report_q4.md`?\n\nFINAL: done',
                 'Please provide the room, date (YYYY-MM-DD), time (HH:MM), and number of people so I can create '
                 '`booking.json`.\n\nFINAL: done')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


def fixture_suite(root, body, register=None, quick=False):
    """A tiny backend/tests stand-in that loads the checkout's proof_log and serial_register plugins."""
    suite = root / 'backend' / 'tests'
    suite.mkdir(parents=True)
    for name in ('proof_log.py', 'serial_register.py', 'pytest.ini'):
        (suite / name).write_text((TESTS / name).read_text())
    (suite / 'conftest.py').write_text('import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n'
                                       "pytest_plugins = ('proof_log', 'serial_register')\n")
    (suite / 'serial_register.json').write_text(json.dumps(register or {}))
    (suite / 'test_fixture.py').write_text(body)
    if quick:
        (suite / 'test_quick.py').write_text(QUICK)
    return suite


def pytest_run(root, *args):
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    return subprocess.run([sys.executable, '-m', 'pytest', 'backend/tests', '-p', 'no:cacheprovider', '-q',
                           '--basetemp', str(root / ('cases-' + str(time.monotonic_ns()))), *args],
                          cwd=root, capture_output=True, text=True, env=env, timeout=300)


def rows(text):
    return [line for line in text.splitlines() if FINALIZER_ROW.match(line)]


class Campaign:
    def __init__(self, scratch, backend=None, only=None):
        self.scratch, self.backend, self.results, self.only = scratch, backend, [], only

    def check(self, name, fn):
        assert name in PLANNED and name not in {r['name'] for r in self.results}, name
        if self.only is not None and name not in self.only:  # the tests run one check at a time
            return
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
        from pebble import release_readiness as rr

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            return {'outside_checkout': True}
        self.check('fixture_scratch_created_outside_the_checkout', created)

        def proof_log():
            root = scratch / 'proof'
            fixture_suite(root, BODY, quick=True)
            serial = pytest_run(root, '--proof-log', str(root / 'serial.log'))
            parallel = pytest_run(root, '-n', '2', '--dist', 'loadfile', '--proof-log', str(root / 'parallel.log'))
            assert serial.returncode == parallel.returncode == 0, (serial.stdout[-600:], parallel.stdout[-600:])
            a, b = (root / 'serial.log').read_text(), (root / 'parallel.log').read_text()
            assert rows(a) == rows(b) and len(rows(b)) == 10, 'the parallel rows differ from the serial ones'
            assert rows(b)[-1].startswith('backend/tests/test_quick.py::test_quick PASSED'), 'not in collection order'
            statuses = [FINALIZER_ROW.match(line).group(3) for line in rows(b)]
            assert statuses.count('PASSED') == 8 and 'SKIPPED' in statuses and 'XFAIL' in statuses, statuses
            parsed = rr.parse_test_log(b.encode())
            assert parsed and parsed['collected'] == 10 and parsed['passed'] == 8 and 'collected 10 items' in b, parsed
            again = pytest_run(root, '--proof-log', str(root / 'parallel.log'))
            assert again.returncode != 0 and '--proof-log already exists' in again.stdout + again.stderr, \
                'an existing proof log was not refused'
            return {'rows': len(rows(b)), 'statuses': sorted(set(statuses)), 'workers': 2}
        self.check('a_parallel_proof_log_equals_the_serial_one', proof_log)

        def runner():
            module = load('run_full_suite_p32', ROOT / 'scripts/run_full_suite.py')

            def drop_kept(out):
                """A failing fixture run keeps its scratch for diagnosis (as a real run should); remove it here."""
                kept = json.loads((out / 'summary.json').read_text()).get('scratch_kept')
                if kept and Path(kept).parent == Path('/private/tmp') and Path(kept).name.startswith('pebble-suite-'):
                    shutil.rmtree(kept, ignore_errors=True)
                return kept
            root = scratch / 'runner'
            fixture_suite(root, BODY, {'test_fixture.py::test_alone': {'reason': 'runs alone in this fixture'}})
            out = root / 'run'
            quiet = contextlib.redirect_stdout(io.StringIO())
            with quiet:
                code = module.main(['--out', str(out), '--workers', '2', '--root', str(root)])
            summary = json.loads((out / 'summary.json').read_text())
            assert drop_kept(out), 'a failed run kept no scratch for diagnosis'
            text = (out / 'proof.log').read_text()
            # A skip and a strict xfail are pytest outcomes the finalizers refuse; the runner's verdict must say so.
            assert code == 1 and not summary['passed'] and summary['collected'] == 9, summary
            assert summary['session_b']['tests'] == 1 and summary['missing'] == summary['in_both_sessions'] == [], summary
            order = [FINALIZER_ROW.match(line).group(2) for line in rows(text)]
            assert order[6] == 'test_alone' and len(order) == 9, order  # collection order, though it ran last
            clean = root / 'clean'
            fixture_suite(clean, BODY.split('@pytest.mark.skip')[0], {'test_fixture.py::test_alone': {'reason': 'alone'}})
            with contextlib.redirect_stdout(io.StringIO()):
                assert module.main(['--out', str(clean / 'run'), '--workers', '2', '--root', str(clean)]) == 0
            good = json.loads((clean / 'run' / 'summary.json').read_text())
            assert good['passed'] and good['outcomes'] == {'PASSED': 7}, good
            parsed = rr.parse_test_log((clean / 'run' / 'proof.log').read_bytes())
            assert parsed['collected'] == parsed['passed'] == 7, parsed
            broken = root / 'broken'
            fixture_suite(broken, BODY.split('@pytest.mark.skip')[0] + '\ndef test_broken():\n    assert False\n',
                          {'test_fixture.py::test_alone': {'reason': 'alone'}})
            with contextlib.redirect_stdout(io.StringIO()):
                assert module.main(['--out', str(broken / 'run'), '--workers', '2', '--root', str(broken)]) == 1
            assert 'FAILED backend/tests/test_fixture.py::test_broken' in (broken / 'run' / 'proof.log').read_text()
            drop_kept(broken / 'run')
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    module.main(['--out', str(clean / 'run'), '--root', str(clean)])
                raise AssertionError('an existing output folder was reused')
            except SystemExit:
                pass
            return {'sessions': ['parallel (-n 2, loadfile, not serial)', 'serial'], 'merged_rows': 7,
                    'refuses': ['a failed test', 'a skip or xfail', 'an existing folder']}
        self.check('the_full_suite_runner_merges_both_sessions_and_fails_on_any_failure', runner)

        def register():
            entries = json.loads((TESTS / 'serial_register.json').read_text())
            assert isinstance(entries, dict), 'the register is not a mapping'
            for key, value in entries.items():
                assert key.startswith('test_') and str((value or {}).get('reason', '')).strip(), ('no reason', key)
            if entries:
                listed = subprocess.run([sys.executable, '-m', 'pytest', 'backend/tests', '--collect-only', '-q',
                                         '-p', 'no:cacheprovider'], cwd=ROOT, capture_output=True, text=True,
                                        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=600)
                assert listed.returncode == 0, listed.stdout[-800:] + listed.stderr[-800:]
                names = {line.strip().rsplit('/', 1)[-1] for line in listed.stdout.splitlines() if '::' in line}
                files = {name.split('::')[0] for name in names}
                for key in entries:
                    assert key in files or any(n == key or n.startswith(key + '[') for n in names), ('stale', key)
            for bad in ({'test_fixture.py::test_gone': {'reason': 'removed'}},
                        {'test_fixture.py::test_alone': {'reason': ' '}}):
                root = scratch / ('register-' + str(len(list(scratch.glob('register-*')))))
                fixture_suite(root, BODY, bad)
                done = pytest_run(root)
                assert done.returncode != 0 and 'serial_register.json' in done.stdout + done.stderr, bad
            return {'entries': len(entries), 'reasons': sorted({v['reason'] for v in entries.values()})[:10],
                    'refused': ['a stale entry', 'an entry without a reason']}
        self.check('the_serial_register_is_true', register)

        def affected():
            module = load('affected_tests_p32', ROOT / 'scripts/affected_tests.py')
            missing = [name for name in module.SAFETY if not (TESTS / name).is_file()]
            assert not missing, ('the safety set names tests that do not exist', missing)
            chosen, reasons, _ = module.select(['backend/pebble/runtime_commands.py'])
            assert 'everything' not in reasons and 'test_runtime_commands.py' in chosen, reasons.get('everything')
            assert set(module.SAFETY) <= set(chosen), 'the safety set is not always selected'
            every = sorted(p.name for p in TESTS.glob('test_*.py'))
            assert len(chosen) < len(every), 'a backend module change selected everything'
            for rel in ('backend/tests/conftest.py', 'backend/tests/serial_register.json', 'backend/requirements-dev.txt',
                        'docs/A_FILE_NO_TEST_READS.md'):
                chosen_all, why, _ = module.select([rel])
                assert chosen_all == every and why.get('everything'), (rel, why)
            changed_test = module.select(['backend/tests/test_faster_tests_32.py'])[0]
            assert 'test_faster_tests_32.py' in changed_test
            return {'module_change_selects': len(chosen), 'of': len(every), 'safety_set': len(module.SAFETY),
                    'everything_for': ['test setup', 'the register', 'the locks', 'an unmapped file']}
        self.check('affected_tests_run_everything_when_unsure', affected)

        def pooled():
            reports, orders = {}, {}
            for jobs in ('1', '3'):
                root = scratch / ('campaign-' + jobs)
                v = load('vhc_p32_' + jobs, ROOT / 'scripts/verify_harness_candidate.py')
                assert v.SERIAL_SUITES >= {'phase18', 'phase24'}, v.SERIAL_SUITES
                for name in ('reports', '.runtime', 'scripts'):
                    (root / name).mkdir(parents=True)
                suites = {f'phase{n}': f'validate_s{n}.py' for n in (90, 91, 92, 93, 94)}
                order = []

                def fake(command, log, order=order):
                    raw = Path(command[command.index('--report') + 1])
                    kind = 'frozen' if '--backend' in command else 'source'
                    order.append((Path(command[1]).name, kind))
                    log.write_text('ok\n')
                    raw.write_text(json.dumps({'passed': True, 'kind': kind, 'checks': ['c1', 'c2'],
                                               'source_sha256': {'a.py': 'x'}, 'backend_sha256': 'e' * 64}))
                    return 0
                v.ROOT, v.SUITES, v.RETIRED = root, suites, {}
                v.PHASES = ('phase90', 'phase91', 'phase92', 'phase93', 'phase94')
                v.SERIAL_SUITES = frozenset({'phase92'})
                v.sources = lambda: {'a.py': 'x'}
                v.candidate_manifest = lambda executable: {'bundle_sha256': 'b' * 64, 'files': []}
                v.run_validator, v.sha = fake, (lambda path: 'e' * 64)
                for name in suites.values():
                    (root / 'scripts' / name).write_text('# stand-in\n')
                (root / 'snapshot.json').write_text(json.dumps({'a.py': 'x'}))
                (root / 'backend').write_bytes(b'x')
                argv = sys.argv
                sys.argv = ['vhc', '--candidate', str(root / 'backend'), '--snapshot', str(root / 'snapshot.json'),
                            '--phase', 'phase94', '--jobs', jobs]
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        v.main()
                finally:
                    sys.argv = argv
                reports[jobs] = sorted(p.name for p in (root / 'reports').iterdir())
                orders[jobs] = order
            assert reports['1'] == reports['3'] and len(reports['1']) == 10, reports
            assert orders['3'][-2:] == [('validate_s92.py', 'source'), ('validate_s92.py', 'frozen')], orders['3']
            return {'reports_equal': True, 'reports': len(reports['1']), 'serial_suite_ran_last_and_alone': True}
        self.check('the_pooled_campaign_gives_the_same_reports', pooled)

        def harness():
            from benchmarks.drivers import chatgpt_limits as cl
            from benchmarks.drivers import claude_cell as cc
            from benchmarks.drivers import codex_cell as cx
            from benchmarks.drivers import workspace as ws
            work = scratch / 'workspace'
            for rel in ('quantiles.py', '__pycache__/quantiles.cpython-312.pyc', '.pytest_cache/v/cache/nodeids',
                        'pkg/__pycache__/m.cpython-312.pyc', 'stale.pyc'):
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                (work / rel).write_text('x')
            assert set(ws.inventory(work)) == {'quantiles.py'}, 'H1: caches are counted as workspace files'
            for reply in SOL_QUESTIONS:
                assert cx.asks_question(reply, changed=False), 'H2: a question with the mandated FINAL line'
                assert not cx.asks_question(reply, changed=True), 'H2: a question that also wrote files'
            assert cx.asks_question('Which should I copy into `summary.md`? I can also include both.')
            assert not cx.asks_question('Booked it. Let me know if you want changes.\nFINAL: done', changed=False)
            assert cc.asks_question is cx.asks_question and cc.signal_group is cx.signal_group
            real = cx.os.killpg

            def zombie(pid, signum):
                raise PermissionError(1, 'Operation not permitted')
            cx.os.killpg = zombie
            try:
                cx.signal_group(2 ** 22, signal.SIGTERM)  # H3: EPERM on a group whose leader exited unreaped
            finally:
                cx.os.killpg = real
            now = 1_800_000_000

            def reading(primary, weekly=10, reached=None):
                return {'primary': {'used_percent': primary, 'window_minutes': 300, 'resets_at': now + 3600},
                        'secondary': {'used_percent': weekly, 'window_minutes': 10080, 'resets_at': now + 86400},
                        'limit_reached': reached, 'ordinary_usage_allowed': True}
            assert cl.decide(reading(79), now=now)['action'] == 'go'
            assert cl.decide(reading(80), now=now) == {'action': 'wait', 'seconds': 3660, 'reason': 'primary_window_at_80'}
            assert cl.decide(reading(10, weekly=90), now=now)['action'] == 'stop'
            assert 'exec resume' not in ' '.join(cx.RESUME_FLAGS) and '--cd' not in cx.RESUME_FLAGS
            resume = cx.command('codex', scratch, scratch / 'last.txt', 'hi', session_id='th-1', model='gpt-6.1-sol')
            assert resume[1:4] == ['exec', 'resume', 'th-1'] and '--cd' not in resume and '--sandbox' not in resume
            return {'H1': 'caches ignored', 'H2': 'asked with FINAL: done and no change', 'H3': 'EPERM tolerated',
                    'usage_rules': {'pause_at': cl.PAUSE_AT, 'stop_weekly_at': cl.STOP_WEEKLY_AT}}
        self.check('benchmark_harness_defects_stay_fixed', harness)


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase32-', dir='/private/tmp')).resolve()
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
