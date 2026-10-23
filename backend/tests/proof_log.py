"""Phase 32 (T2): the serial-format proof log of a test run, with or without pytest-xdist.

The release finalizers read a full suite's verbose log: one `backend/tests/x.py::test PASSED [ nn%]` row per test,
pytest's `collected N items` line, the `FAILED`/`ERROR` short-summary lines and the final `= N passed in Ts =` summary.
pytest-xdist prints its rows in completion order with worker tags, so this plugin writes the classic rows itself, in
collection order, from the same run's reports (`pytest_runtest_logreport`, which the xdist controller receives for
every test), to the new file that `--proof-log` names. A run without xdist writes the same file. A test that never
reported has no row, so a crashed or partial run stays visibly incomplete (rows fewer than collected).
"""
import time
from pathlib import Path

import pytest

RANK = {'PASSED': 0, 'XFAIL': 1, 'SKIPPED': 2, 'XPASS': 3, 'FAILED': 4, 'ERROR': 5}
HEADER = '# pebble proof log (backend/tests/proof_log.py): pytest\'s own reports of this run, one row per test in collection order'


def pytest_addoption(parser):
    parser.addoption('--proof-log', default=None, metavar='FILE',
                     help='write the serial-format proof log of this run to FILE, a new file')


def pytest_configure(config):
    path = config.getoption('--proof-log')
    if path and not hasattr(config, 'workerinput'):  # the plain run or the xdist controller, never a worker
        config.pluginmanager.register(ProofLog(config, Path(path)), 'pebble-proof-log')


class ProofLog:
    def __init__(self, config, path):
        if path.exists() or path.is_symlink():
            raise pytest.UsageError('--proof-log already exists: ' + str(path))
        self.config, self.path = config, path
        self.order, self.outcomes, self.deselected = None, {}, 0
        self.started = time.time()

    def pytest_deselected(self, items):
        self.deselected += len(items)

    def pytest_collection_finish(self, session):
        if self.order is None and not hasattr(self.config, 'workerinput') and session.items:
            self.order = [item.nodeid for item in session.items]

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        if self.order is None:
            self.order = list(ids)  # every worker collects the same list; xdist checks they agree

    def pytest_runtest_logreport(self, report):
        if report.when == 'call':
            if hasattr(report, 'wasxfail'):
                status = 'XPASS' if report.passed else 'XFAIL'
            else:
                status = report.outcome.upper()
        elif report.failed:
            status = 'ERROR'  # a setup or teardown error
        elif report.skipped:
            status = 'SKIPPED'  # skipped at setup
        else:
            return
        old = self.outcomes.get(report.nodeid)
        if old is None or RANK[status] > RANK[old]:
            self.outcomes[report.nodeid] = status

    def pytest_sessionfinish(self, session, exitstatus):
        order = self.order or []
        relative = self.config.cwd_relative_nodeid
        rows, counts = [], {}
        for index, nodeid in enumerate(order, 1):
            status = self.outcomes.get(nodeid)
            if status is None:
                continue
            counts[status] = counts.get(status, 0) + 1
            rows.append(f'{relative(nodeid)} {status} [{index * 100 // len(order):3d}%]')
        words = [(counts.get('PASSED', 0), 'passed'), (counts.get('FAILED', 0), 'failed'),
                 (counts.get('SKIPPED', 0), 'skipped'), (counts.get('XFAIL', 0), 'xfailed'),
                 (counts.get('XPASS', 0), 'xpassed'), (counts.get('ERROR', 0), 'errors'), (self.deselected, 'deselected')]
        summary = ', '.join(f'{n} {word}' for n, word in words if n or word == 'passed')
        failures = [f'{"ERROR" if self.outcomes[n] == "ERROR" else "FAILED"} {relative(n)}' for n in order
                    if self.outcomes.get(n) in ('FAILED', 'ERROR')]
        lines = [HEADER, f'collected {len(order)} items', *rows, *failures,
                 f'{"=" * 16} {summary} in {time.time() - self.started:.2f}s {"=" * 16}']
        with self.path.open('x', encoding='utf-8') as out:
            out.write('\n'.join(lines) + '\n')
