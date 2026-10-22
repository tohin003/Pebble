"""Phase 20 fault campaign (T4, first piece): validator contract and live source-mode scenarios.

The contract tests are fast and start no backend. The live tests run the validator CLI against the source
backend in pytest's own temporary folder (TMPDIR), with its loopback fault provider only. Merge (i20): the four
findings (F7-1, F7-2, F8-1, F8-2) were fixed in extensions.py, controller.py and memory.py, so their checks are plain
tests now; the strict xfail marks, the patch list and its private-copy test are gone.
Clean-up (i20b): the explicit-request tests that ran the whole campaign or one scenario repeated the validator's own
run (verify_harness_candidate runs validate_phase20_faults.py, every scenario, in source and frozen modes), so they
were removed and the default run has no skip. f8-lock-brief, the one scenario outside LIVE, is graded by that run.
"""

import contextlib
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))  # the validator imports its sibling validators by name
_spec = importlib.util.spec_from_file_location('validate_phase20_faults', SCRIPTS / 'validate_phase20_faults.py')
faults = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(faults)

ALL_CHECKS = [name for _, names in faults.SCENARIOS.values() for name in names]


# Contract ------------------------------------------------------------------------------------------------------

def test_planned_names_are_unique_and_findings_name_planned_checks():
    every = list(faults.COMMON_FIRST) + ALL_CHECKS + list(faults.COMMON_LAST)
    assert len(every) == len(set(every))
    assert faults.COMMON_LAST[-1] == 'backend_inputs_stable_during_campaign'
    assert set(faults.FINDINGS) <= set(ALL_CHECKS)
    assert len({finding for finding, _ in faults.FINDINGS.values()}) == len(faults.FINDINGS)
    middle = faults.planned(['f8-lock-held', 'f3-status'])[1:-len(faults.COMMON_LAST)]
    assert middle == faults.SCENARIOS['f8-lock-held'][1] + faults.SCENARIOS['f3-status'][1]


def test_selection_is_validated_ordered_and_fits_the_verifier_limit():
    assert faults.select_scenarios(None) == list(faults.SCENARIOS)
    assert faults.select_scenarios('f8-lock-held, f3-status') == ['f3-status', 'f8-lock-held']
    for bad in ('f3-nothing', '', 'f3-status,f3-status', 'f3-status,,f6-child'):
        with pytest.raises(ValueError):
            faults.select_scenarios(bad)
    total = sum(bound for bound, _ in faults.SCENARIOS.values()) + faults.MARGIN_SECONDS
    assert total <= faults.VERIFIER_SECONDS == 240, total
    original = dict(faults.SCENARIOS)
    try:
        faults.SCENARIOS['f3-status'] = (faults.VERIFIER_SECONDS, faults.SCENARIOS['f3-status'][1])
        with pytest.raises(ValueError, match='verifier limit'):
            faults.select_scenarios('f3-status')
    finally:
        faults.SCENARIOS.clear()
        faults.SCENARIOS.update(original)


def test_an_existing_report_or_link_is_refused_and_never_overwritten(tmp_path, capsys):
    existing = tmp_path / 'report.json'
    existing.write_text('earlier evidence\n')
    link = tmp_path / 'link.json'
    link.symlink_to(tmp_path / 'missing-target.json')
    for path in (existing, link):
        with pytest.raises(SystemExit):
            faults.main(['--report', str(path), '--scenarios', 'f3-status'])
        assert 'already exists' in capsys.readouterr().err
    assert existing.read_text() == 'earlier evidence\n' and not (tmp_path / 'missing-target.json').exists()
    with pytest.raises(FileExistsError):
        faults.write_new(existing, 'replacement')
    with pytest.raises(OSError):
        faults.write_new(link, 'through a link')
    assert not (tmp_path / 'missing-target.json').exists()
    fresh = tmp_path / 'fresh.json'
    faults.write_new(fresh, '{}\n')
    assert fresh.read_text() == '{}\n' and (fresh.stat().st_mode & 0o777) == 0o600
    with pytest.raises(SystemExit):
        faults.main(['--report', str(tmp_path / 'other.json'), '--scenarios', 'f3-unknown'])
    assert not (tmp_path / 'other.json').exists()


def test_route_tags_roots_and_children_and_ignores_host_text():
    mark = faults.MARK
    root = [{'role': 'system', 'content': 'synthetic'},
            {'role': 'user', 'content': 'RETRIEVED MEMORY: an older goal ' + mark + 'f3a'},
            {'role': 'user', 'content': 'Reply once. ' + mark + 'f3b'},
            {'role': 'user', 'content': 'HOST COMPLETION CHECK: quoted ' + mark + 'f3c'}]
    assert faults.route(root) == ('f3b', 'root')
    child = [{'role': 'user', 'content': faults.ASSIGNMENT + json.dumps({'task': 'Summarise. ' + mark + 'f6c'})}]
    assert '\\u00a7' in child[0]['content'] and faults.route(child) == ('f6c', 'child')
    assert faults.route([{'role': 'user', 'content': 'No tag here.'}]) == (None, 'root')


def _post(url, body):
    request = urllib.request.Request(url + '/v1/chat/completions', data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


def test_fault_provider_serves_each_scripted_fault_on_loopback_only():
    provider = faults.FaultProvider()
    assert provider.server.server_address[0] == '127.0.0.1'
    provider.start()
    try:
        date = 'Wed, 21 Oct 2026 07:28:00 GMT'
        call = faults.tool('UIRender', 'dropped', {'kind': 'diagram'})
        provider.script('f9a', 'root', [('status', 429, 1), ('status', 429, date), ('status', 401, None),
                                        ('drop', ['one ', 'two '], call), ('final', 'done'),
                                        lambda results, host: ('final', 'saw ' + ','.join(results))])
        body = {'model': faults.MODEL, 'messages': [{'role': 'user', 'content': 'Go. ' + faults.MARK + 'f9a'}]}
        assert _post(provider.url, body)[:2][0] == 429 and _post(provider.url, body)[1]['Retry-After'] == date
        status, headers, text = _post(provider.url, body)
        assert status == 401 and 'HTTP 401' in text and 'Retry-After' not in headers
        status, _, text = _post(provider.url, body)
        assert status == 200 and text.count('data: ') == 3 and '[DONE]' not in text and '"dropped"' in text
        status, _, text = _post(provider.url, body)
        assert status == 200 and text.rstrip().endswith('data: [DONE]') and '"done"' in text
        answered = {**body, 'messages': body['messages'] + [{'role': 'tool', 'tool_call_id': 'c1', 'content': '{"ok": 1}'}]}
        assert '"saw c1"' in _post(provider.url, answered)[2]
        assert _post(provider.url, body)[0] == 200  # beyond the script: answered, and flagged
        assert [a['reason'] for a in provider.anomalies] == ['unscripted request']
        assert _post(provider.url, {**body, 'model': 'another-model'})[0] == 500
        assert _post(provider.url, {**body, 'messages': [{**body['messages'][0], '_pebble_x': 1}]})[0] == 500
        assert len(provider.errors) == 2
        assert [r['action'] for r in provider.matching(tag='f9a')][:6] == [
            'status-429', 'status-429', 'status-401', 'drop', 'final', 'final']
    finally:
        provider.stop()


def test_a_stall_holds_the_stream_until_the_client_goes_away():
    provider = faults.FaultProvider()
    provider.start()
    try:
        provider.script('f9b', 'root', [('stall', ['partial '])])
        body = json.dumps({'model': faults.MODEL, 'messages': [{'role': 'user', 'content': faults.MARK + 'f9b'}]}).encode()
        with contextlib.closing(socket.create_connection(('127.0.0.1', provider.server.server_port), 5)) as sock:
            sock.sendall(b'POST /v1/chat/completions HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: '
                         + str(len(body)).encode() + b'\r\n\r\n' + body)
            received = b''
            while b'partial' not in received:
                received += sock.recv(4096)
            time.sleep(0.3)
            assert 'ended' not in provider.matching(tag='f9b')[0]  # still holding the stream open
        closed = time.monotonic()
        while 'ended' not in provider.matching(tag='f9b')[0] and time.monotonic() - closed < 5:
            time.sleep(0.05)
        record = provider.matching(tag='f9b')[0]
        assert record['ended'] - closed < 2 and not provider.anomalies, (record, provider.anomalies)
    finally:
        provider.stop()


def test_locker_holds_an_exclusive_lock_for_a_bounded_time(tmp_path):
    path = tmp_path / 'pebble.sqlite'
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE t(x)')
        db.commit()
    locker = faults.Locker(path, hold=None, cap=1.0)
    locker.acquire()
    with contextlib.closing(sqlite3.connect(path, timeout=0.2)) as other:
        assert other.execute('SELECT count(*) FROM t').fetchone() == (0,)  # WAL readers continue
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            other.execute('INSERT INTO t VALUES(1)')
        assert locker.released.wait(3) and 0.9 <= locker.facts['held_seconds'] < 2.5, locker.facts
        other.execute('INSERT INTO t VALUES(1)')
        other.commit()
    early = faults.Locker(path, hold=30.0, cap=60.0)
    early.acquire()
    early.release()
    assert early.released.wait(3) and early.facts['held_seconds'] < 1 and 'error' not in early.facts


def _raises(exc):
    def fn():
        raise exc
    return fn


def _offline_campaign(selection):
    campaign = faults.Campaign(['unused'], selection)
    campaign.provider.server.server_close()
    campaign.trap.server.server_close()
    return campaign


def test_unplanned_duplicate_and_unrun_checks_never_pass_and_findings_need_a_failure(tmp_path):
    campaign = _offline_campaign(['f3-status', 'f8-lock-held'])
    with pytest.raises(AssertionError):
        campaign.check('an_unplanned_check', lambda: None)
    assert campaign.check('f3s_a_401_is_never_retried', lambda: {'requests': 1})
    with pytest.raises(AssertionError):
        campaign.check('f3s_a_401_is_never_retried', lambda: None)
    finding = 'f8_the_run_never_stays_running_after_the_service_has_settled_it'
    assert not campaign.check(finding, _raises(AssertionError({'run_state_with_the_service_idle': 'running'})))
    with pytest.raises(faults.ScenarioTimeout):
        campaign.check('f8_a_lock_held_through_settlement_keeps_waits_bounded', _raises(faults.ScenarioTimeout('bound passed')))
    campaign.observations['time_bound_exceeded'] = {'f8-lock-held': 'ScenarioTimeout: bound passed'}
    campaign.finish()
    sources = {'backend/main.py': 'a' * 64}
    report = faults.build_report(campaign, backend=None, backend_sha=None, sources=sources, sources_after=dict(sources),
                                 duration=1.0, scratch=tmp_path)
    assert report['kind'] == 'source' and report['passed'] is False and report['backend_sha256'] is None
    assert report['all_planned_checks_recorded'] and report['planned_checks'] == list(faults.planned(['f3-status', 'f8-lock-held']))
    assert report['checks'] == ['f3s_a_401_is_never_retried', 'backend_inputs_stable_during_campaign']
    assert len(set(report['failed'])) == len(report['failed']) == len(report['planned_checks']) - 2
    assert [f['id'] for f in report['findings']] == ['F8-1'] and 'running' in report['findings'][0]['evidence']
    unrun = {c['name']: c for c in report['check_results'] if c.get('not_run')}
    restart = 'f8_a_restart_keeps_a_settled_run_and_recovers_a_stranded_one_as_interrupted'
    assert restart in unrun and 'bound passed' in unrun[restart]['error']
    assert 'fixture_scratch_created_outside_the_checkout' in unrun
    assert report['skipped_cases']['f3_dns_failure'] and report['duration_budget_seconds'] == 240
    changed = faults.build_report(_offline_campaign(['f3-status']), backend=None, backend_sha=None,
                                  sources=sources, sources_after={'backend/main.py': 'b' * 64}, duration=1.0, scratch=tmp_path)
    assert 'backend_inputs_stable_during_campaign' in changed['failed']


def test_the_report_carries_what_the_parent_verifier_reads():
    text = (SCRIPTS / 'verify_harness_candidate.py').read_text()
    for key in ("report.get('passed')", "report.get('kind')", "report.get('checks')", "report.get('backend_sha256')",
                "report.get('source_sha256'"):
        assert key in text, key
    assert 'timeout=240' in text and faults.VERIFIER_SECONDS == 240


# Live source-mode scenarios --------------------------------------------------------------------------------------

def _campaign(tmp_path, scenarios):
    """The CLI on a selection of scenarios, as the parent verifier runs it, in pytest's own TMPDIR."""
    report = tmp_path / 'report.json'
    with (tmp_path / 'campaign.log').open('w') as sink:
        done = subprocess.run([sys.executable, str(SCRIPTS / 'validate_phase20_faults.py'), '--report', str(report),
                               '--scenarios', ','.join(scenarios)],
                              cwd=ROOT, env={**os.environ, 'TMPDIR': str(tmp_path)},
                              stdout=sink, stderr=subprocess.STDOUT, timeout=faults.VERIFIER_SECONDS + 60)
    outcome = json.loads(report.read_text())
    assert outcome['kind'] == 'source' and outcome['all_planned_checks_recorded'] is True
    assert outcome['scenarios'] == scenarios
    assert outcome['duration_seconds'] < faults.VERIFIER_SECONDS
    assert Path(outcome['scratch_preserved']).is_relative_to(tmp_path)
    assert (done.returncode == 0) is outcome['passed']
    return outcome


# Every scenario except f8-lock-brief (about 11 s), which the validator's own run grades, keeps this file near 90 s.
LIVE =['f3-retry-after', 'f3-status', 'f3-stream', 'f6-child', 'f7-extension', 'f8-lock-held']
LIVE_CHECKS = [name for scenario in LIVE for name in faults.SCENARIOS[scenario][1]]
# Finding -> (its check, a fragment of the failure the unfixed checkout produced). Another failure is a real failure.
SIGNATURES = {
    'F7-1': ('f7_startup_survives_an_extension_row_with_corrupt_config_and_reports_it_disabled', 'extensions.py'),
    'F7-2': ('f7_a_skill_whose_snapshot_no_longer_verifies_is_disabled_and_reported_at_startup', "'enabled': True"),
    'F8-1': ('f8_the_run_never_stays_running_after_the_service_has_settled_it',
             "'run_state_with_the_service_idle': 'running'"),
    'F8-2': ('f8_a_status_read_under_the_lock_answers_without_a_database_error', 'database is locked'),
}


@pytest.fixture(scope='module')
def live(tmp_path_factory):
    outcome = _campaign(tmp_path_factory.mktemp('live'), LIVE)
    return {**outcome, 'results': {c['name']: c for c in outcome['check_results']}}


def test_live_campaign_records_every_check_and_fails_only_known_findings(live):
    assert live['fatal_error'] is None and live['planned_checks'] == list(faults.planned(LIVE))
    unexpected = sorted(set(live['failed']) - set(faults.FINDINGS))
    assert not unexpected, {name: live['results'][name].get('error') for name in unexpected}
    assert [f['check'] for f in live['findings']] == [n for n in live['planned_checks'] if n in live['failed']]
    assert all(f['evidence'] for f in live['findings'])
    assert live['passed'] is (not live['failed']) and not live['provider_errors'] and not live['provider_anomalies']
    for name, bound in live['scenario_bounds_seconds'].items():
        assert live['observations']['scenario_seconds'][name] <= bound, (name, live['observations']['scenario_seconds'])
    assert not any(s.get('stopped_through_live_handle') for s in live['observations']['services'])


@pytest.mark.parametrize('name', [n for n in LIVE_CHECKS if n not in faults.FINDINGS] + list(faults.COMMON_LAST))
def test_live_check_passes(live, name):
    result = live['results'][name]
    assert result['passed'], result.get('error')


# Merge (i20): the parent applied the fixes for all four findings, so each finding's check must pass (no xfail).
@pytest.mark.parametrize('finding', list(SIGNATURES))
def test_live_finding(live, finding):
    name, signature = SIGNATURES[finding]
    assert faults.FINDINGS[name][0] == finding
    result = live['results'][name]
    if not result['passed'] and signature not in (result.get('error') or ''):
        raise RuntimeError(finding + ' failed for another reason: ' + (result.get('error') or '')[:800])
    assert result['passed'], result.get('error')
