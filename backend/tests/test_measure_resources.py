"""Phase 18 E06: the model-free resource sampler and scenario plumbing in scripts/measure_resources.py.

The sampler is proven on short-lived children these tests start and reap. No
model, network beyond loopback, credential or user data is used. Measurements
of the frozen candidate come later, from the parent.
"""

import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('measure_resources', ROOT / 'scripts/measure_resources.py')
mr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mr)

darwin = pytest.mark.skipif(sys.platform != 'darwin', reason='libproc and the stock tools are macOS only')


def test_nearest_rank_is_the_benchmark_definition():
    values = list(range(1, 21))
    assert mr.nearest_rank(values, 0.95) == 19 and mr.nearest_rank(values, 0.5) == 10
    assert mr.nearest_rank(values, 1.0) == 20 and mr.nearest_rank([7.5], 0.95) == 7.5
    assert mr.nearest_rank(list(reversed(values)), 0.05) == 1
    assert mr.nearest_rank([], 0.95) is None
    for share in (0, -0.1, 1.01):
        with pytest.raises(ValueError):
            mr.nearest_rank(values, share)
    assert mr.latency([3.0, None, 1.0, 2.0]) == {'n': 3, 'p50': 2.0, 'p95': 3.0, 'max': 3.0}
    assert mr.latency([]) == {'n': 0}


def test_environment_allowlist_is_exactly_the_native_shell_list():
    source = (ROOT / 'src-tauri/src/main.rs').read_text()
    listed = re.search(r'for key in \[([^\]]*)\]', source).group(1)
    assert tuple(re.findall(r'"([A-Za-z_]+)"', listed)) == mr.ENV_ALLOWLIST


def test_rusage_info_v2_offsets_follow_the_struct_layout():
    fields = ('user_time', 'system_time', 'pkg_idle_wkups', 'interrupt_wkups', 'pageins', 'wired_size',
              'resident_size', 'phys_footprint', 'proc_start_abstime', 'proc_exit_abstime', 'child_user_time',
              'child_system_time', 'child_pkg_idle_wkups', 'child_interrupt_wkups', 'child_pageins',
              'child_elapsed_abstime', 'diskio_bytesread', 'diskio_byteswritten')
    for name, offset in mr.OFFSETS.items():
        assert offset == 16 + 8 * fields.index(name), name
    # The same footprint offset as the Phase 14 memory governor.
    from pebble import job_shim
    assert mr.OFFSETS['phys_footprint'] == job_shim.RI_PHYS_FOOTPRINT_OFFSET
    assert mr.PROC_PPID_ONLY == job_shim.PROC_PPID_ONLY and mr.RUSAGE_INFO_V2 == job_shim.RUSAGE_INFO_V2


@darwin
def test_sampler_self_test_on_owned_children(tmp_path):
    """X4: tree walk, footprint, ri_diskio bytes and the CPU time unit agree with wait4 on a real child."""
    result = mr.self_test(tmp_path)
    assert result['passed'] is True, json.dumps(result)
    evidence = result['evidence']
    assert evidence['members_max'] >= 2 and 0.75 <= evidence['cpu_ratio'] <= 1.33
    assert not list(tmp_path.iterdir()), 'the self-test removes its own file'


class FakeLibProc:
    """A scripted process table: {pid: (parent, counters)}."""

    available = True
    timebase = (1, 1)
    error = None

    def __init__(self):
        self.table = {}

    def set(self, pid, parent, start, cpu_ns, footprint, read=0, written=0, wakeups=0):
        self.table[pid] = (parent, {'user_time_ns': cpu_ns, 'system_time_ns': 0, 'pkg_idle_wkups': wakeups,
                                    'interrupt_wkups': 0, 'resident_size': footprint, 'phys_footprint': footprint,
                                    'proc_start_abstime': start, 'diskio_bytesread': read,
                                    'diskio_byteswritten': written})

    def rusage(self, pid):
        return dict(self.table[pid][1]) if pid in self.table else None

    def tree(self, root):
        if root not in self.table:
            return None
        members, frontier = [root], [root]
        while frontier:
            parent = frontier.pop()
            for pid, (owner, _) in self.table.items():
                if owner == parent and pid not in members:
                    members.append(pid)
                    frontier.append(pid)
        return members


def test_sampler_keeps_exited_members_and_treats_a_reused_pid_as_new():
    lib, now = FakeLibProc(), [0.0]
    lib.set(10, 1, start=100, cpu_ns=1_000_000_000, footprint=50 * mr.MB, written=10)
    lib.set(11, 10, start=200, cpu_ns=500_000_000, footprint=20 * mr.MB, written=5, wakeups=4)
    sampler = mr.Sampler(10, libproc=lib, clock=lambda: now[0])
    sampler.started = 0.0
    first = sampler.sample()
    assert first['members'] == 2 and first['footprint'] == 70 * mr.MB
    now[0] = 1.0
    lib.set(11, 10, start=200, cpu_ns=700_000_000, footprint=20 * mr.MB, written=7, wakeups=6)
    sampler.sample()
    now[0] = 2.0
    del lib.table[11]                                   # the child exits: its last counters stay counted
    lib.set(10, 1, start=100, cpu_ns=1_100_000_000, footprint=40 * mr.MB, written=10)
    third = sampler.sample()
    assert third['members'] == 1 and third['user_time_ns'] == 1_800_000_000
    now[0] = 4.0
    lib.set(11, 10, start=999, cpu_ns=100_000_000, footprint=30 * mr.MB)   # pid 11 reused by a new process
    fourth = sampler.sample()
    assert fourth['user_time_ns'] == 1_900_000_000 and len(sampler.keys()) == 3
    summary = sampler.summary()
    assert summary['samples'] == 4 and summary['window_seconds'] == 4.0 and summary['members_max'] == 2
    assert summary['cpu_seconds'] == 0.4 and summary['cpu_percent_of_one_core'] == 10.0
    assert summary['footprint_bytes']['peak'] == 70 * mr.MB and summary['footprint_bytes']['last'] == 70 * mr.MB
    assert summary['disk_written_bytes'] == 2 and summary['wakeups'] == 2
    # Liveness is (pid, start time): the reused pid does not make the old child alive.
    assert sorted(sampler.alive()) == [10, 11] and sampler.alive([(11, 200)]) == []
    del lib.table[10]
    assert sampler.sample() is None and sampler.summary()['unreadable_samples'] == 1


def test_summarize_with_no_or_one_sample():
    assert mr.summarize([]) == {'samples': 0, 'unreadable_samples': 0}
    one = mr.summarize([{'t': 0, 'members': 1, 'footprint': 5, 'resident': 6, **dict.fromkeys(mr.COUNTERS, 3)}])
    assert one['cpu_percent_of_one_core'] is None and one['wakeups_per_second'] is None and one['cpu_seconds'] == 0


@darwin
def test_reap_reports_maxrss_and_kills_only_an_unreaped_leader(tmp_path):
    child = subprocess.Popen([sys.executable, '-I', '-c',
                              'import sys\nblock = bytearray(48 << 20)\nfor i in range(0, len(block), 4096): block[i] = 1\n'
                              'sys.stdin.read()'], stdin=subprocess.PIPE)
    time.sleep(0.5)
    child.stdin.close()
    record = mr.reap(child, 30)
    assert record['exit_code'] == 0 and record['killed_after_seconds'] is None and record['maxrss_bytes'] >= 40 << 20
    assert child.returncode == 0
    stubborn = subprocess.Popen([sys.executable, '-I', '-c', 'import time\nwhile True: time.sleep(1)'],
                                stdin=subprocess.PIPE)
    stubborn.stdin.close()
    record = mr.reap(stubborn, 0.5)
    assert record['killed_after_seconds'] == 0.5 and record['exit_code'] == -9


LSOF_SAMPLE = '\n'.join([
    'p4242', 'f5', 'PTCP', 'n127.0.0.1:50001->127.0.0.1:8080',
    'f6', 'PTCP', 'n127.0.0.1:8081',
    'f7', 'PTCP', 'n[::1]:50002->[::1]:9000',
    'p4243', 'f3', 'PTCP', 'n*:5000',
    'f4', 'PUDP', 'n*:*',
    'f8', 'PTCP', 'n192.168.1.20:50003->93.184.216.34:443',
    'f9', 'PTCP', 'n127.0.0.1:50004->10.0.0.1:443', '']) + '\n'


def test_lsof_parser_counts_wildcards_and_remote_ends_as_non_loopback():
    parsed = mr.parse_lsof(LSOF_SAMPLE)
    assert parsed['connections'] == 7 and parsed['loopback'] == 3 and parsed['non_loopback'] == 4
    assert parsed['wildcard'] == 2
    assert 'TCP 192.168.1.20:50003->93.184.216.34:443' in parsed['non_loopback_names']
    assert mr.parse_lsof('') == {'connections': 0, 'loopback': 0, 'non_loopback': 0, 'wildcard': 0,
                                 'non_loopback_names': []}


@darwin
def test_lsof_snapshot_of_an_owned_loopback_listener():
    if not os.path.isfile(mr.TOOLS['lsof']):
        pytest.skip('lsof is not installed')
    listener = socket.socket()
    try:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        snapshot = mr.lsof_snapshot([os.getpid()], 'self')
    finally:
        listener.close()
    assert snapshot['status'] == 'measured' and snapshot['connections'] >= 1
    assert snapshot['non_loopback'] == 0, snapshot
    assert mr.lsof_snapshot([], 'none')['status'] == 'blocked'


def test_nettop_and_top_parsers():
    csv = ('time,,bytes_in,bytes_out,\n'
           '12:00:00.1,Python.4242,1000,250,\n'
           '12:00:00.1,Other.77,99999,99999,\n'
           '12:00:00.1,pebble-backend.4243,24,6,\n')
    assert mr.parse_nettop(csv, [4242, 4243]) == {'bytes_in': 1024, 'bytes_out': 256, 'rows': 2}
    assert mr.parse_nettop('no header here', [1]) is None
    assert mr.parse_nettop('time,,bytes_in,bytes_out,\n1,Python.5,lots,1,\n', [5]) is None
    top = ('Processes: 600 total\nPID    POWER\n4242   3.5\n\nProcesses: 600 total\nPID    POWER\n4242   0.4\n')
    assert mr.parse_top_power(top, 4242) == 0.4 and mr.parse_top_power(top, 1) is None


@darwin
def test_nettop_positive_control_on_an_owned_loopback_connection():
    """X2: per-process nettop bytes without root, against a known loopback transfer in this process."""
    if not os.path.isfile(mr.TOOLS['nettop']):
        pytest.skip('nettop is not installed')
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname(), timeout=5)
    server, _ = listener.accept()
    try:
        client.sendall(b'x' * 200_000)
        received = 0
        while received < 200_000:
            received += len(server.recv(65536))
        result = mr.nettop_bytes([os.getpid()])
    finally:
        for item in (client, server, listener):
            item.close()
    import warnings
    warnings.warn('X2 nettop positive control: ' + json.dumps(result))
    assert result['status'] in ('measured', 'inconclusive', 'blocked')
    if result['status'] == 'measured':
        assert result['bytes_in'] + result['bytes_out'] >= 200_000, result


def test_missing_tools_are_blocked_never_guessed(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, 'TOOLS', {name: str(tmp_path / name) for name in mr.TOOLS})
    assert mr.lsof_snapshot([os.getpid()])['status'] == 'blocked'
    assert mr.nettop_bytes([os.getpid()])['status'] == 'blocked'
    assert mr.top_power(os.getpid())['status'] == 'blocked'
    assert mr.power_source()['status'] == 'blocked'
    assert mr.BATTERY['status'] == 'blocked' and 'sudo' in mr.BATTERY['reason']
    # No code path runs sudo or powermetrics.
    source = (ROOT / 'scripts/measure_resources.py').read_text()
    assert not re.search(r"\[\s*'sudo'|'/usr/bin/sudo'|TOOLS\[.powermetrics", source)


def test_disk_usage_sums_blocks_and_never_follows_links(tmp_path):
    home = tmp_path / 'home'
    (home / 'nested').mkdir(parents=True)
    (home / 'nested' / 'a.bin').write_bytes(b'x' * 10_000)
    outside = tmp_path / 'outside.bin'
    outside.write_bytes(b'y' * 1_000_000)
    (home / 'link.bin').symlink_to(outside)
    (home / 'pebble.sqlite').write_bytes(b'z' * 4096)
    usage = mr.home_usage(home)
    assert 10_000 <= usage['apparent_bytes'] < 100_000 and usage['allocated_bytes'] >= 10_000
    assert usage['database_bytes'] == {'pebble.sqlite': 4096, 'pebble.sqlite-wal': None, 'pebble.sqlite-shm': None}
    assert usage['telemetry'] == {'exists': False, 'bytes': 0}
    (home / 'telemetry').mkdir()
    (home / 'telemetry' / 'queue.jsonl').write_bytes(b'{}\n')
    assert mr.telemetry_footprint(home) == {'exists': True, 'bytes': 3}


def test_reports_are_created_exclusively(tmp_path):
    path = tmp_path / 'report.json'
    mr.write_new(path, {'a': 1})
    with pytest.raises(FileExistsError):
        mr.write_new(path, {'a': 2})
    assert json.loads(path.read_text()) == {'a': 1}
    link = tmp_path / 'link.json'
    link.symlink_to(tmp_path / 'elsewhere.json')
    with pytest.raises(FileExistsError):
        mr.write_new(link, {'a': 3})
    assert not (tmp_path / 'elsewhere.json').exists()


def test_cli_refuses_existing_reports_bad_scenarios_and_unsafe_backends(tmp_path, capsys):
    existing = tmp_path / 'old.json'
    existing.write_text('{}')
    for argv in (['run', '--report', str(existing)],
                 ['run', '--report', str(tmp_path / 'new.json'), '--scenarios', 'r1,r9'],
                 ['run', '--report', str(tmp_path / 'new.json'), '--scenarios', 'r1,r1'],
                 ['run', '--report', str(tmp_path / 'new.json'), '--runs', '0'],
                 ['run', '--report', str(tmp_path / 'missing' / 'new.json')],
                 ['run', '--report', str(tmp_path / 'new.json'), '--scratch', str(ROOT / 'scratch-inside')],
                 ['run', '--report', str(tmp_path / 'new.json'), '--scratch', str(tmp_path)],
                 ['watch', '--report', str(tmp_path / 'new.json'), '--pid', '1'],
                 ['run', '--report', str(tmp_path / 'new.json'), '--backend', str(tmp_path / 'absent')]):
        with pytest.raises(SystemExit) as stopped:
            mr.main(argv)
        assert stopped.value.code == 2, argv
    shared = tmp_path / 'shared-backend'
    shared.write_text('#!/bin/sh\n')
    shared.chmod(0o777)
    with pytest.raises(SystemExit):
        mr.main(['run', '--report', str(tmp_path / 'new.json'), '--backend', str(shared)])
    linked = tmp_path / 'linked-backend'
    linked.symlink_to(Path(sys.executable))
    with pytest.raises(SystemExit):
        mr.main(['run', '--report', str(tmp_path / 'new.json'), '--backend', str(linked)])
    assert not (tmp_path / 'new.json').exists() and not (ROOT / 'scratch-inside').exists()


def _messages(tag, tools=0):
    messages = [{'role': 'system', 'content': 'synthetic'}, {'role': 'user', 'content': 'goal ' + tag}]
    for index in range(tools):
        messages += [{'role': 'assistant', 'content': None}, {'role': 'tool', 'tool_call_id': str(index), 'content': '{}'}]
    return messages


def test_provider_scripts_follow_each_family_and_serve_sse_on_loopback():
    provider = mr.Provider(scripts={'probe': lambda messages: {'content': 'probe'}})
    tag = mr.MARK + 'r2-coding-4'
    names = [provider.reply({'messages': _messages(tag, n)})[1] for n in range(3)]
    assert json.loads(names[0]['tool_calls'][0]['function']['arguments']) == {'file_path': 'notes.py'}
    assert names[1]['tool_calls'][0]['function']['name'] == 'Edit' and 'FINAL:' in names[2]['content']
    research = [provider.reply({'messages': _messages(mr.MARK + 'r2-research-1', n)})[1] for n in range(3)]
    assert [r.get('tool_calls', [{}])[0].get('function', {}).get('name') for r in research] == ['Read', 'Read', None]
    assert provider.reply({'messages': _messages(mr.MARK + 'r2-memory-2')}) == (mr.MARK + 'r2-memory-2',
                                                                                mr.script_memory([]))
    assert provider.reply({'messages': _messages(mr.MARK + 'r2-probe-0')})[1] == {'content': 'probe'}
    assert provider.reply({'messages': _messages('no tag')})[0] is None
    provider.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = json.dumps({'model': 'resource-fixture', 'messages': _messages(tag)}).encode()
        request = urllib.request.Request(provider.url + '/chat/completions', data=body, method='POST',
                                         headers={'Content-Type': 'application/json'})
        with opener.open(request, timeout=10) as response:
            stream = response.read().decode()
        assert stream.startswith('data: ') and stream.rstrip().endswith('data: [DONE]') and '"Read"' in stream
        assert provider.seconds(tag) >= 0 and len(provider.records) == 1 and not provider.errors
        assert provider.url.startswith('http://127.0.0.1:')
    finally:
        provider.stop()


def test_egress_trap_refuses_and_records_only_host_and_port():
    """Requests through the trap never leave loopback: the proxy answers 403 itself (no DNS, no upstream)."""
    trap = mr.EgressTrap().start()
    try:
        env = trap.environment()
        assert {env[name] for name in mr.PROXY_NAMES} == {trap.url} and trap.url.startswith('http://127.0.0.1:')
        assert env['NO_PROXY'] == env['no_proxy'] == '127.0.0.1,localhost,::1'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': trap.url, 'https': trap.url}))
        for url in ('http://tokenizer.invalid/encodings/o200k_base.tiktoken?sig=canary-secret',
                    'https://upload.invalid/telemetry'):
            with pytest.raises(OSError):
                opener.open(url, timeout=10)
        report = trap.report()
        assert report['attempts'] == 2 and report['targets'] == ['tokenizer.invalid:80', 'upload.invalid:443']
        assert 'canary-secret' not in json.dumps(report) and 'o200k' not in json.dumps(report)
    finally:
        trap.stop()


def test_resources_preflight_reports_a_missing_tokenizer(tmp_path):
    assert mr.resources_preflight(tmp_path)['complete'] is False
    (tmp_path / 'tokenizer').mkdir()
    (tmp_path / 'tokenizer' / 'fffe').write_bytes(b'x')
    assert mr.resources_preflight(tmp_path) == {'tokenizer_files': 1, 'complete': True, 'note': None}


def _scenarios(outside=0, telemetry=0, lsof='measured'):
    snapshot = {'status': lsof, 'non_loopback': outside}
    return {
        'r1_idle': {'idle': {'window_seconds': 600, 'cpu_percent_of_one_core': 0.4,
                             'footprint_bytes': {'p50': 200 * mr.MB, 'peak': 210 * mr.MB}},
                    'network': {'lsof': [snapshot]}, 'home_after_exit': {'telemetry': {'bytes': telemetry}}},
        'r2_scripted': {'load': {'footprint_bytes': {'peak': 1100 * mr.MB}}, 'runs': 50,
                        'home_growth_mb_per_100_runs': 12.5, 'network': {'lsof': [snapshot]},
                        'home_after_exit': {'telemetry': {'bytes': 0}}},
        'r3_cold_start': {'first_response_seconds': {'n': 10, 'p95': 4.2},
                          'trials': [{'lsof': snapshot, 'telemetry': {'bytes': 0}}]},
    }


def test_threshold_rows_follow_the_design_study_values():
    rows = mr.thresholds(_scenarios())
    assert rows['T11_idle_footprint_mb_p50'] == {'measured': 200.0, 'limit': 300, 'met': True, 'blocking': False,
                                                 'planned_idle_seconds': 600, 'idle_seconds': 600}
    assert rows['T11_peak_footprint_mb']['met'] is False and rows['T11_peak_footprint_mb']['measured'] == 1100.0
    assert rows['T11_idle_cpu_percent_of_one_core']['met'] is True
    assert rows['T11_home_growth_mb_per_100_runs']['met'] is True
    assert rows['T11_non_loopback_connections']['met'] is True and rows['T11_non_loopback_connections']['blocking']
    assert rows['T11_telemetry_bytes_by_default']['met'] is True
    assert rows['T8_cold_start_p95_seconds']['met'] is True and rows['T8_cold_start_p95_seconds']['blocking_above'] == 10
    assert mr.thresholds(_scenarios(outside=1))['T11_non_loopback_connections']['met'] is False
    assert mr.thresholds(_scenarios(telemetry=5))['T11_telemetry_bytes_by_default']['met'] is False
    blocked = mr.thresholds(_scenarios(lsof='blocked'))['T11_non_loopback_connections']
    assert blocked['met'] is None and blocked['blocked_snapshots'] == 3
    trapped = mr.thresholds(_scenarios(), {'attempts': 2})['T11_non_loopback_connections']
    assert trapped['met'] is False and trapped['measured'] == 2 and trapped['egress_attempts_refused'] == 2
    assert mr.thresholds(_scenarios(lsof='blocked'), {'attempts': 1})['T11_non_loopback_connections']['met'] is False
    empty = mr.thresholds({})
    assert all(row['met'] is None for row in empty.values())


@darwin
def test_source_service_scenarios_on_explicit_request(request, tmp_path):
    """Starts backend/main.py: R3 (2 trials), R1 (3 s idle) and R2 (3 scripted runs). Name it to run it."""
    # Runs in the full suite since the Phase 18 merge (the evidence finalizer allows no skipped test).
    report = tmp_path / 'resources.json'
    done = subprocess.run([sys.executable, str(ROOT / 'scripts/measure_resources.py'), 'run', '--report', str(report),
                           '--idle-seconds', '3', '--runs', '3', '--trials', '2', '--scratch', str(tmp_path / 'scratch'),
                           '--label', 'source smoke'], cwd=ROOT, capture_output=True, timeout=600)
    outcome = json.loads(report.read_text())
    summary = {name: {**{k: v for k, v in s.items() if k in ('error', 'states', 'runs', 'first_response_seconds',
                                                            'latency_seconds', 'still_running_after_exit',
                                                            'energy_proxy', 'cpu_seconds_per_run')},
                      'nettop': (s.get('network') or {}).get('nettop')}
               for name, s in outcome['scenarios'].items()}
    summary['power_source'] = outcome['power_source']
    summary['egress_trap'] = outcome['egress_trap']
    import warnings
    warnings.warn('resource smoke: ' + json.dumps({'thresholds': outcome['thresholds'], 'scenarios': summary,
                                                   'errors': outcome['errors'], 'stable': outcome['inputs_stable'],
                                                   'exit': done.returncode})[:12000])
    assert outcome['kind'] == 'source' and outcome['complete'] is True, (done.stdout[-2000:], done.stderr[-2000:])
    assert outcome['privacy_passed'] is True and outcome['inputs_stable'] is True
    assert outcome['egress_trap']['attempts'] == 0, outcome['egress_trap']
    assert done.returncode == 0
    r2 = outcome['scenarios']['r2_scripted']
    assert r2['runs'] == 3 and r2['approvals'] >= 1 and 'waiting_approval' not in r2['states'], r2['states']
    assert outcome['scenarios']['r1_idle']['idle']['samples'] >= 3
    assert outcome['scenarios']['r1_idle']['idle']['footprint_bytes']['peak'] > 0
    assert outcome['scratch_preserved'] is None and not (tmp_path / 'scratch').exists()


# --- scripts/validate_phase18.py -------------------------------------------------------------------

def _validator():
    spec = importlib.util.spec_from_file_location('validate_phase18', ROOT / 'scripts/validate_phase18.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raise(error):
    raise error


def test_validator_checks_are_planned_unique_and_pending_is_never_passed():
    v = _validator()
    assert len(set(v.PLANNED)) == len(v.PLANNED) and v.PLANNED[-1] == 'backend_inputs_stable_during_campaign'
    campaign = v.Campaign([sys.executable, '-c', 'pass'], None)
    assert campaign.check('telemetry_off_by_default', lambda: _raise(v.Pending('routes'))) is False
    assert campaign.check('r2_scripted_loopback_only', lambda: {'ok': 1}) is True
    assert campaign.check('r1_idle_loopback_only_and_sampler_reads_service', lambda: 1 / 0) is False
    with pytest.raises(AssertionError):
        campaign.check('r2_scripted_loopback_only', lambda: None)       # unique
    with pytest.raises(AssertionError):
        campaign.check('made_up_check', lambda: None)                   # planned
    campaign.finish()
    by_name = {c['name']: c for c in campaign.checks}
    assert set(by_name) == set(v.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert by_name['telemetry_off_by_default']['passed'] is False and by_name['telemetry_off_by_default']['gap'] == 'routes'
    assert campaign.gaps == {'routes'} and 'error' in by_name['r1_idle_loopback_only_and_sampler_reads_service']
    ran = {'telemetry_off_by_default', 'r2_scripted_loopback_only', 'r1_idle_loopback_only_and_sampler_reads_service'}
    assert all(c.get('not_run') and not c['passed'] for n, c in by_name.items() if n not in ran)
    assert all(v.TOKEN in c for c in v.CANARIES)


def test_validator_route_check_is_pending_until_the_parent_integrates_the_routes():
    v = _validator()
    from pebble import tool_authz, telemetry
    campaign = v.Campaign([sys.executable, '-c', 'pass'], None)
    allowed, pet = v.native_route_lists()
    assert not pet & set(telemetry.ROUTES)
    assert v.component_routes() == {"'" + r + "'" for r in telemetry.ROUTES}
    if set(telemetry.ROUTES) <= tool_authz.NATIVE_ONLY and set(telemetry.ROUTES) <= allowed:
        assert campaign.routes_native_only()['routes'] == sorted(telemetry.ROUTES)
    else:
        with pytest.raises(v.Pending):
            campaign.routes_native_only()


def test_validator_scripts_probe_the_routes_and_plant_canaries():
    v = _validator()
    first = v.script_probe([{'role': 'user', 'content': 'x'}])
    assert first['tool_calls'][0]['function']['name'] == 'telemetry_set'
    second = v.script_probe([{'role': 'tool'}])
    assert second['tool_calls'][0]['function']['name'] == 'telemetry_export'
    assert 'content' in v.script_probe([{'role': 'tool'}, {'role': 'tool'}])
    final = v.script_canary([{'role': 'tool'}])['content']
    assert v.CANARY_OUTPUT in final and v.CANARY_HOST in final and v.CANARY_SECRET in final


@darwin
def test_validator_source_campaign_on_explicit_request(request, tmp_path):
    """The whole validator in source mode. Before the parent integrates the native routes, the hook and the
    destination token, the telemetry checks are pending (never passed) and nothing fails.

    TMPDIR is inherited, not a fresh folder: in a copy whose src-tauri/resources lacks the tokenizer, a fresh
    TMPDIR has no tiktoken cache and the first run would try to download one (the egress trap refuses it)."""
    # Runs in the full suite since the Phase 18 merge (the evidence finalizer allows no skipped test).
    v = _validator()
    report = tmp_path / 'phase18.json'
    done = subprocess.run([sys.executable, str(ROOT / 'scripts/validate_phase18.py'), '--report', str(report)], cwd=ROOT,
                          capture_output=True, timeout=v.VERIFIER_SECONDS + 60)
    outcome = json.loads(report.read_text())
    import warnings
    warnings.warn('phase18 campaign: ' + json.dumps({
        'seconds': outcome['duration_seconds'], 'passed': outcome['checks'], 'pending': outcome['pending'],
        'failed': outcome['failed'], 'fatal': outcome['fatal_error'], 'gaps': sorted(outcome['integration_gaps']),
        'errors': {c['name']: c.get('error') for c in outcome['check_results'] if c.get('error')},
        'evidence': {c['name']: c.get('evidence') for c in outcome['check_results'] if c['passed']}})[:12000])
    assert outcome['kind'] == 'source' and outcome['all_planned_checks_recorded'] is True
    assert outcome['planned_checks'] == list(v.PLANNED) and outcome['duration_seconds'] < v.VERIFIER_SECONDS
    assert outcome['fatal_error'] is None and outcome['failed'] == [], outcome['failed']
    assert set(outcome['pending']) <= {n for n in v.PLANNED if n.startswith('telemetry_')}
    assert done.returncode == (1 if outcome['pending'] else 0) and outcome['passed'] is (not outcome['pending'])
    assert outcome['backend_sha256'] is None and 'backend/pebble/telemetry.py' in outcome['source_sha256']
    assert outcome['observations']['egress_trap']['attempts'] == 0, outcome['observations']['egress_trap']


@darwin
def test_cold_tokenizer_cache_egress_is_trapped_on_explicit_request(request, tmp_path):
    """A fresh TMPDIR (no tiktoken cache). With the tokenizer bundled in src-tauri/resources nothing tries the
    network; without it the first run's tokenizer download is refused by the trap, recorded and fails R2."""
    # Runs in the full suite since the Phase 18 merge (the evidence finalizer allows no skipped test).
    v = _validator()
    report = tmp_path / 'phase18.json'
    subprocess.run([sys.executable, str(ROOT / 'scripts/validate_phase18.py'), '--report', str(report)], cwd=ROOT,
                   env={**os.environ, 'TMPDIR': str(tmp_path)}, capture_output=True, timeout=v.VERIFIER_SECONDS + 60)
    outcome = json.loads(report.read_text())
    trap, resources = outcome['observations']['egress_trap'], outcome['observations']['resources']
    import warnings
    warnings.warn('cold tokenizer cache: ' + json.dumps({'trap': trap, 'resources': resources,
                                                         'failed': outcome['failed']}))
    assert not (tmp_path / 'data-gym-cache').exists() or resources['complete'], 'a download reached the cache'
    if resources['complete']:
        assert trap['attempts'] == 0 and outcome['failed'] == []
    else:
        assert trap['attempts'] >= 1 and any(t.endswith(':443') for t in trap['targets']), trap
        assert 'r2_scripted_loopback_only' in outcome['failed'] and outcome['passed'] is False
