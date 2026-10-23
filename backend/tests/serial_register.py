"""Phase 32 (T1, T3): tests that must run with nothing else beside them.

backend/tests/serial_register.json maps a test file (`test_x.py`) or one test (`test_x.py::test_y`) to the measured
reason it cannot share the machine: a per-user process limit, a wall-clock bound, a system-wide measurement. Each
collected test it names gets the `serial` marker, so scripts/run_full_suite.py runs it in its serial session, after the
parallel one. An entry without a reason, or naming nothing that was collected, stops the run: the register stays true.
Nothing here skips a test or changes what it checks.
"""
import json
from pathlib import Path

import pytest

REGISTER = Path(__file__).with_name('serial_register.json')


def load(path=REGISTER):
    entries = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(entries, dict):
        raise pytest.UsageError('serial_register.json must map tests to their reasons')
    for key, value in entries.items():
        if not isinstance(value, dict) or not str(value.get('reason', '')).strip() or not key.startswith('test_'):
            raise pytest.UsageError('serial_register.json: ' + key + ' needs a test file or node and a reason')
    return entries


def pytest_configure(config):
    config.addinivalue_line('markers', 'serial: runs alone, after the parallel session (backend/tests/serial_register.json)')


def pytest_collection_modifyitems(session, config, items):
    entries = load()
    used = set()
    for item in items:
        name = item.nodeid.rsplit('/', 1)[-1]  # test_x.py::test_y[param]
        file, _, test = name.partition('::')
        for key in (file, file + '::' + test.split('[')[0], name):
            if key in entries:
                item.add_marker(pytest.mark.serial)
                used.add(key)
    selection = config.getoption('file_or_dir') or []
    whole_suite = not selection or any(Path(arg).name == 'tests' for arg in selection)
    unused = sorted(set(entries) - used)
    if unused and whole_suite and not config.getoption('markexpr'):
        raise pytest.UsageError('serial_register.json names tests that were not collected: ' + ', '.join(unused[:6]))
