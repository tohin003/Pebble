"""Phase 24 (t24-bench): short scripted versions of PB1-PB5.

These check that each benchmark measures what it says on the real code (the App on a scripted ChatGPT route, and
backend/main.py over JSON-RPC on a structured scratch home in a fake HOME), with tiny sizes. They make no timing
claim: the numbers are printed, never asserted against the 1.0 targets, except where the measured structure itself is
the prediction (PB5's mirror count, i6-perf-01; a mismatch is a finding). No model, credential or network is used:
this process refuses sockets, and the backend runs behind the egress tripwire.
"""

import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.perf import pb  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('These tests must not open network connections.')
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ('127.0.0.1', '::1'):
            refuse()
        return connect(self, address)
    monkeypatch.setattr(socket.socket, 'connect', guarded)


def measured(item):
    return item['n'] >= 1 and item['median'] is not None and 0 <= item['median'] <= item['p95'] <= item['max']


def test_pb1_send_to_first_request_by_project_size(tmp_path):
    result = pb.pb1(tmp_path, sizes=(10, 60), samples=3)
    print('\nPB1', json.dumps(result))
    assert set(result) == {'10', '60'} and all(measured(v) and v['n'] == 3 for v in result.values())


def test_pb2_host_time_per_step_fresh_and_after_prior_edits(tmp_path):
    result = pb.pb2(tmp_path, steps=4, prior_edits=(0, 30), budgets=(32768,))
    print('\nPB2', json.dumps(result))
    assert set(result) == {'32K-0', '32K-30'} and all(measured(v) and v['n'] == 4 for v in result.values())


def test_pb3_store_scale_over_json_rpc(tmp_path):
    result = pb.pb3(tmp_path, chats=(5, 25), samples=3)
    print('\nPB3', json.dumps(result))
    for count in ('5', '25'):
        assert all(measured(result[count][key]) for key in ('state', 'copy_pass', 'stop', 'new_chat'))
        assert result[count]['egress_attempts'] == 0
    assert result['targets']['at_1000'] == {'copy_pass_s': 0.5, 'state_s': 0.05, 'stop_s': 1.0}


def test_pb4_idle_cpu_of_the_backend(tmp_path):
    result = pb.pb4(tmp_path, chats=(25,), window=1.0, warmup=0.5)
    print('\nPB4', json.dumps(result))
    item = result['25']
    assert item['cpu_percent_of_core'] is not None and item['cpu_percent_of_core'] >= 0
    assert item['window_seconds'] >= 1.0 and item['egress_attempts'] == 0


def test_pb5_memory_and_mirrors_over_runs(tmp_path):
    from pebble.guard import GuardAdapter
    result = pb.pb5(tmp_path, runs=4, files=40)
    print('\nPB5', json.dumps({k: v for k, v in result.items() if k != 'series'}))
    assert len(result['series']) == 4 and all(s['rss_mb'] for s in result['series'])
    # The structure is the prediction: 0.6.1 keeps one mirror per run until the backend exits (i6-perf-01); t24-gate's
    # lifecycle keeps at most one per project root. A different count is a finding, not a number to adjust.
    if hasattr(GuardAdapter, 'end') and hasattr(GuardAdapter, 'unstage'):
        assert result['max_mirrors'] <= 1
    else:
        assert [s['mirrors'] for s in result['series']] == [1, 2, 3, 4]


def test_the_cli_writes_a_new_quick_report_with_the_machine(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('bench_perf', ROOT / 'scripts/bench_perf.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, 'machine', lambda power=True: {'platform': sys.platform, 'power': 'not read in tests'})
    report = tmp_path / 'perf.json'
    assert cli.main(['run', '--report', str(report), '--pb', '1', '--quick', '--scratch', str(tmp_path / 's')]) == 0
    data = json.loads(report.read_text())
    assert data['schema'] == cli.SCHEMA and data['quick'] is True and set(data['results']) == {'PB1'}
    assert set(data['delegated']) == {'PB6', 'PB7', 'PB8', 'PB9', 'PB10'}
    assert cli.main(['run', '--report', str(report), '--pb', '1', '--quick']) == 2  # never overwritten
    assert cli.main(['run', '--report', str(tmp_path / 'x.json'), '--pb', '7']) == 2
