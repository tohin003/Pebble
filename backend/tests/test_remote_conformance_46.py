"""Phase 46 (1.9.29, D214): the iOS side's own conformance checker against this bridge.

bridge-mock/conformance.py (the other session's, read-only, loaded without writing into its folder) plays the iPhone
against the bridge on loopback, started by scripts/remote_bridge_dev.py exactly as the lead runs it: a real Pebble App
in a test home with a scripted model (conformance sends one message), remote.folders on a test tree, screenshots of a
test pattern, and the person's confirmation of the six digits (X-1) given after 1.5 s. The two C6 rows need a LAN
address and --bonjour, so they SKIP here, as they do against the mock; everything else must PASS.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest

from remote_kit import FakeAwake, load

conformance = load('conformance')
pytestmark = pytest.mark.skipif(conformance is None, reason="the iOS side's conformance.py is not on this Mac")
ROOT = Path(__file__).resolve().parents[2]
C6 = ('C6: at most 10 handshakes a minute from one address', 'C6: the Bonjour instance is named by the bridge id')


def dev_script():
    spec = importlib.util.spec_from_file_location('remote_bridge_dev_under_test', ROOT / 'scripts/remote_bridge_dev.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dev(tmp_path, monkeypatch):
    from pebble import remote_mac
    monkeypatch.setattr(remote_mac, 'KeepAwake', FakeAwake)
    monkeypatch.setattr(remote_mac, 'battery', lambda: {'battery_percent': None, 'charging': False, 'on_power': True})
    started = []

    def start(confirm):
        lines = []
        app, hub = dev_script().start(tmp_path / 'home', tmp_path / 'tree', confirm=confirm, say=lines.append)
        started.append((app, hub))
        return hub, lines
    yield start
    for app, hub in started:
        hub.close()
        app.close()


def test_conformance_passes_every_row_against_the_bridge(dev, tmp_path, capsys):
    (tmp_path / 'tree' / 'Projects' / 'shop' / '.git').mkdir(parents=True)
    (tmp_path / 'tree' / 'Notes').mkdir()
    hub, lines = dev('auto')
    link = hub.remote_pair_start()['link']
    checker = conformance.Checker(link, endpoint=('127.0.0.1', hub.bridge.port), timeout=15, run_timeout=120,
                                  pair_wait=30)
    ok = asyncio.run(checker.run())
    report = capsys.readouterr().out
    failed = [(r['name'], r['detail']) for r in checker.results if r['ok'] is False]
    assert failed == [], report
    skipped = [r['name'] for r in checker.results if r['ok'] is None]
    # The C6 rows need a LAN address; a later proposal's rows (v0.6 and on, not in this bridge's brief) skip by design.
    assert set(C6) <= set(skipped) and not [name for name in skipped if name not in C6 and
                                            not name.startswith(('v0.6', 'v0.7'))], skipped
    assert ok and sum(1 for r in checker.results if r['ok'] is True) >= 47
    assert any(line.startswith('PAIRING_CODE=') for line in lines)
    assert hub.status()['devices'] == [], 'the checker removed its own phone at the end'


def test_conformance_refused_pairing_rows_pass_when_the_person_cancels(dev, capsys):
    hub, lines = dev('refuse')
    link = hub.remote_pair_start()['link']
    checker = conformance.Checker(link, endpoint=('127.0.0.1', hub.bridge.port), timeout=15, pair_wait=30,
                                  expect_pair_refused=True)
    ok = asyncio.run(checker.run())
    report = capsys.readouterr().out
    assert ok and [r['ok'] for r in checker.results] == [True, True, True], report
    assert hub.status()['devices'] == []
