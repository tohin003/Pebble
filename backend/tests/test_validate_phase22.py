"""Phase 22 track v22-validator: scripts/validate_phase22.py, the phase22 service validator.

The unit tests pin the report contract (unique planned names in campaign order, a new report created with O_EXCL,
source digests inside the verifier's snapshot), the proposed verifier registration, the static checks (each also
against a hostile copy), the scripted provider (its bytes are read by Pebble's own Responses stream parser), the
loopback black hole on this machine, and the file, database and Markdown helpers. The vault-helper and Chrome checks
run as written against their fakes: a fake helper process, a sealed fake extension over the real bridge socket, and
the real backend executable as the native host against a fake bridge socket.

Two command-line tests drive main(): a small campaign (static checks and the legacy home on one real service, every
other scenario a no-op) once as the source run and once through an owned --backend launcher around the same source
backend (the frozen plumbing, not a frozen binary), and the whole campaign once on the source service. No model
inference and no network: every model turn is the validator's scripted loopback provider. The verifier runs the
validator itself, in source and frozen modes.

Two product defects the validator found are pinned with their proposed patches (track note): the native host aborts
when Pebble ends the relay (b6), and staged edits fail in a ~/.pebble home (r2, s5). Each test expects the defect while
the patch is absent and the clean result once it is applied.
"""

import importlib.util
import io
import json
import shutil
import socket
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))  # the validator imports its sibling validators by name
_spec = importlib.util.spec_from_file_location('validate_phase22', SCRIPTS / 'validate_phase22.py')
v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v)


def load_script(name):
    spec = importlib.util.spec_from_file_location(name + '_for_phase22_validator', SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stub(backend=None, command=('unused',)):
    """A campaign that never starts its provider, its trap or a service."""
    campaign = v.Campaign(list(command), backend)
    campaign.provider.stop()
    campaign.trap.server.server_close()
    return campaign


def results(campaign):
    return {c['name']: c for c in campaign.checks}


def failures(campaign, names):
    found = results(campaign)
    return {name: (found.get(name) or {}).get('error', 'not recorded')[:1500] for name in names
            if not (found.get(name) or {}).get('passed')}


# The report contract --------------------------------------------------------------------------------------------

def test_planned_names_are_unique_ordered_and_every_scenario_is_bounded():
    assert len(v.PLANNED) == len(set(v.PLANNED)) and v.PLANNED[-1] == 'backend_inputs_stable_during_campaign'
    assert v.PLANNED[0] == 'fixture_scratch_created_outside_the_checkout'
    assert set(v.SCENARIO_BOUNDS) == set(v.SCENARIOS) and all(0 < b < v.BUDGET_SECONDS for b in v.SCENARIO_BOUNDS.values())
    assert v.BUDGET_SECONDS < v.VERIFIER_SECONDS == 240
    # Every planned area of the track has its checks: the stall watchdog, the idle limit, reasoning summaries, usage,
    # run_activity, the data home, the Documents copies, settings.json, the vault helper, the browser and the release.
    prefixes = {name.split('_')[0] for name in v.PLANNED}
    assert {'w1', 'w2', 'i1', 'i2', 'r1', 'r2', 'r3', 'u1', 'u2', 'u3', 'a1', 'a2', 'a3', 'm1', 'm2', 'm3', 'm4', 'm5',
            'd1', 'd2', 'd3', 'd4', 'd5', 's2', 's3', 's4', 'k1', 'k2', 'k3', 'b1', 'b2', 'b3', 'b4', 'b5', 'v1',
            'v2'} <= prefixes
    # The stall run waits for the loopback first-event deadline in the background: its result scenario comes after
    # every service scenario and its bound covers the wait.
    order = list(v.SCENARIOS)
    assert order.index('stall_start') < order.index('stream') < order.index('stall_result') < order.index('restart')
    assert v.SCENARIO_BOUNDS['stall_result'] > v.STALL_WAIT > v.LOOPBACK_FIRST_EVENT
    assert v.HOLD_STALL >= v.STALL_WAIT and v.HOLD_IDLE > v.IDLE_GAP[1]


def test_the_report_is_a_new_file_and_every_source_it_hashes_is_in_the_verifier_snapshot(tmp_path, monkeypatch, capsys):
    verify = load_script('verify_harness_candidate')
    # Names only: this copy need not hold .runtime/phase19-baseline/build_epoch8_fixture.py, which sources() hashes.
    monkeypatch.setattr(verify, 'sha', lambda path: None)
    covered = set(verify.sources())
    digests = v.checkout_digests()
    assert set(digests) <= covered, sorted(set(digests) - covered)
    assert all(isinstance(value, str) and len(value) == 64 for value in digests.values())
    for name in ('scripts/validate_phase22.py', 'scripts/validate_phase21.py', 'src-tauri/tauri.conf.json',
                 'package.json', 'backend/pebble/__init__.py', 'backend/pebble/run_activity.py',
                 'backend/pebble/data_home.py', 'backend/pebble/vault.py', 'backend/pebble/native_host.py',
                 'src-tauri/src/main.rs', 'backend/main.py'):
        assert name in digests, name
    existing, link = tmp_path / 'report.json', tmp_path / 'link.json'
    existing.write_text('earlier evidence\n')
    link.symlink_to(tmp_path / 'missing-target.json')
    for path in (existing, link):
        with pytest.raises(SystemExit):
            v.main(['--report', str(path)])
        assert 'already exists' in capsys.readouterr().err
    with pytest.raises(FileExistsError):
        v.write_new(existing, 'replacement')
    with pytest.raises(OSError):
        v.write_new(link, 'through a link')
    assert existing.read_text() == 'earlier evidence\n' and not (tmp_path / 'missing-target.json').exists()
    target = tmp_path / 'pebble-backend'
    target.write_text('#!/bin/sh\n')
    target.chmod(0o755)
    (tmp_path / 'backend-link').symlink_to(target)
    for given, expected in ((tmp_path / 'backend-link', 'symbolic link'), (tmp_path / 'absent', 'not readable')):
        with pytest.raises(SystemExit):
            v.main(['--report', str(tmp_path / 'new.json'), '--backend', str(given)])
        assert expected in capsys.readouterr().err
    assert not (tmp_path / 'new.json').exists()
    campaign = stub()
    campaign.finish()
    report = v.build_report(campaign, backend=None, backend_sha=None, sources=digests, sources_after=dict(digests),
                            duration=1.0, scratch=tmp_path, keep=True)
    assert report['kind'] == 'source' and report['backend_sha256'] is None and report['source_sha256'] == digests
    assert [c['name'] for c in report['check_results']] == list(v.PLANNED) and report['all_planned_checks_recorded']
    assert report['passed'] is False and report['checks'] == ['backend_inputs_stable_during_campaign']
    assert report['duration_budget_seconds'] == 240 and report['scratch_preserved'] == str(tmp_path)
    changed = dict(digests, **{'scripts/validate_phase22.py': '0' * 64})
    campaign = stub()
    campaign.finish()
    moved = v.build_report(campaign, backend=None, backend_sha=None, sources=digests, sources_after=changed,
                           duration=1.0, scratch=tmp_path, keep=False)
    assert 'backend_inputs_stable_during_campaign' in moved['failed'] and moved['scratch_preserved'] is None
    campaign = stub(target)
    campaign.finish()
    frozen = v.build_report(campaign, backend=target, backend_sha='0' * 64, sources=digests, sources_after=dict(digests),
                            duration=1.0, scratch=tmp_path, keep=True)
    assert frozen['kind'] == 'frozen' and frozen['backend_sha256'] == '0' * 64
    assert 'backend_inputs_stable_during_campaign' in frozen['failed']


@pytest.mark.parametrize('applied', [False, True], ids=['proposed', 'applied'])
def test_the_proposed_phase22_registration_runs_only_in_the_phase22_namespace(applied):
    """The parent patch (track note): SUITES gains 'phase22' and PHASES gains 'phase22' right after 'phase21'. Applied
    here to a loaded copy of the verifier only; no earlier campaign changes and the verifier's 240 s limit is the
    validator's budget. Once the parent has applied the patch, the applied registration must be the proposed one.
    Located by position, so a later phase appended after phase22 keeps this test unchanged."""
    verify = load_script('verify_harness_candidate')
    if applied and 'phase22' not in verify.SUITES:
        verify.SUITES, verify.PHASES = {**verify.SUITES, 'phase22': 'validate_phase22.py'}, (*verify.PHASES, 'phase22')
    if 'phase22' in verify.SUITES or 'phase22' in verify.PHASES:
        rank = verify.PHASES.index('phase22')
        assert verify.SUITES.get('phase22') == 'validate_phase22.py' and verify.PHASES[rank - 1] == 'phase21'
    # phase22 and every namespace after it leave the tables; phase22 is then appended again.
    cut = verify.PHASES.index('phase22') if 'phase22' in verify.PHASES else len(verify.PHASES)
    dropped = tuple(verify.PHASES[cut:]) or ('phase22',)
    suites = {name: script for name, script in verify.SUITES.items() if not name.startswith(dropped)}
    phases = tuple(verify.PHASES[:cut])
    verify.SUITES, verify.PHASES = suites, phases
    before = {phase: verify.default_suites(phase) for phase in phases}
    verify.SUITES, verify.PHASES = {**suites, 'phase22': 'validate_phase22.py'}, (*phases, 'phase22')
    after = {phase: verify.default_suites(phase) for phase in verify.PHASES}
    assert {phase: after[phase] for phase in before} == before
    assert after['phase22'] == before['phase21'] + ['phase22']
    later = verify.PHASES[verify.PHASES.index('phase21') + 1:]
    assert [phase for phase in later if 'phase22'.startswith(phase)] == ['phase22']  # refused in the phase21 namespace
    assert (SCRIPTS / verify.SUITES['phase22']).is_file()
    source = (SCRIPTS / 'verify_harness_candidate.py').read_text(encoding='utf-8')
    assert 'process.wait(timeout=240)' in source and v.VERIFIER_SECONDS == 240


def test_checks_are_planned_unique_and_not_run_is_never_passed():
    campaign = stub()
    assert campaign.check('fixture_scratch_created_outside_the_checkout', lambda: campaign.need('missing')) is False
    assert 'Prerequisite' in campaign.checks[-1]['error']
    for bad in ('fixture_scratch_created_outside_the_checkout', 'an_unplanned_check'):
        with pytest.raises(AssertionError):
            campaign.check(bad, lambda: None)
    campaign.observations['scenario_errors'] = {'stream': 'Not started: its bound no longer fits.'}
    campaign.finish()
    recorded = results(campaign)
    assert set(recorded) == set(v.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert not any(c['passed'] for c in campaign.checks)
    assert all('bound no longer fits' in recorded[name]['error'] for name in v.SCENARIOS['stream'])


def test_a_scenario_that_no_longer_fits_the_budget_is_not_started():
    campaign = stub()
    ran = []
    campaign.deadline = v.time.monotonic() + v.SCENARIO_BOUNDS['stream'] - 1
    campaign.scenario('stream', lambda: ran.append(True))
    assert not ran and 'Not started' in campaign.observations['scenario_errors']['stream']
    campaign.scenario('static', lambda: ran.append(True))
    assert ran == [True] and 'static' in campaign.observations['scenario_seconds']


# Static checks ----------------------------------------------------------------------------------------------------

def test_static_checks_pass_on_this_checkout(monkeypatch):
    import pebble
    # Phase 23 (the person's decision, 2026-10-14): validate_phase22 pins RELEASE = '0.6.0' and is retired from the
    # Phase 23 campaign on, superseded by validate_phase23 (the same checks, the release from pebble.VERSION). Its
    # constant stays exactly as published; its other static checks are still run here against the current release.
    assert v.RELEASE == '0.6.0'
    monkeypatch.setattr(v, 'RELEASE', pebble.VERSION)
    campaign = stub()
    campaign.static_checks()
    assert [c['name'] for c in campaign.checks] == list(v.SCENARIOS['static'])
    assert all(c['passed'] for c in campaign.checks), json.dumps([c.get('error') for c in campaign.checks])[:3000]
    declared = campaign.checks[0]['evidence']
    assert declared['version'] == pebble.VERSION and set(declared['declarations'].values()) == {pebble.VERSION}


def test_the_version_checks_fail_on_an_older_release(monkeypatch):
    monkeypatch.setattr(v, 'checkout_version', lambda: '0.5.0')
    campaign = stub()
    campaign.static_checks()
    found = results(campaign)['v1_every_release_declaration_names_0_6_0_as_pebble_version']
    assert not found['passed'] and '0.5.0' in found['error'], found


def test_the_route_check_fails_when_the_pet_reaches_a_storage_route(tmp_path, monkeypatch):
    text = v.MAIN_RS.read_text(encoding='utf-8')
    changed = text.replace('&& !["state",', '&& !["state","storage_summary",', 1)
    assert changed != text, 'main.rs no longer has the pet-list anchor this test edits.'
    hostile = tmp_path / 'main.rs'
    hostile.write_text(changed, encoding='utf-8')
    monkeypatch.setattr(v, 'MAIN_RS', hostile)
    campaign = stub()
    campaign.static_checks()
    found = results(campaign)['s1_phase22_routes_are_main_window_routes_and_never_model_tools']
    assert not found['passed'] and 'the pet window reaches' in found['error'], found


def test_the_service_version_check_fails_on_an_older_backend(tmp_path, monkeypatch):
    class Older:
        facts, user_home, label = {}, tmp_path / 'user-home', 'stall'

        @staticmethod
        def rpc(method, **params):
            if method == 'state':
                return {'version': '0.5.0', 'data_dir': str(tmp_path / 'flat-home')}
            raise RuntimeError('fixture: ' + method)
    campaign = stub()
    campaign.facts['scratch'] = tmp_path
    monkeypatch.setattr(v.Campaign, 'service', lambda self, label, **options: Older)
    campaign.scenario('stall_start', campaign.scenario_stall_start)
    found = results(campaign)['v2_the_running_service_reports_release_0_6_0']
    assert not found['passed'] and '0.5.0' in found['error']
    assert 'stall_run' not in campaign.facts  # the stall was never sent; w1 reports a missing prerequisite


# The scripted provider ----------------------------------------------------------------------------------------------

def body(goal, *, outputs=(), host=(), tools=('UpdatePlan', 'Write')):
    items = [{'role': 'user', 'content': goal}]
    items += [{'type': 'function_call_output', 'call_id': call, 'output': '{"status": "error"}'} for call in outputs]
    items += [{'role': 'user', 'content': text} for text in host]
    return {'model': v.MODEL, 'input': items, 'store': False, 'stream': True,
            'tools': [{'type': 'function', 'name': name, 'parameters': {}} for name in tools]}


def test_route_reads_the_human_goal_and_never_host_text_or_recalled_memory():
    tagged = 'Reply please ' + v.MARK + 'u1'
    assert v.route(body(tagged)) == 'u1'
    assert v.route(body(tagged, host=['HOST RESUME: about ' + v.MARK + 'r2', 'RETRIEVED MEMORY ' + v.MARK + 'l1'])) == 'u1'
    assert v.route({'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'x ' + v.MARK + 'i2'}]}]}) == 'i2'
    view = v.View('r2', 1, body(tagged, outputs=['call_r2_write'], host=['HOST RESUME: Continue.']))
    assert view.resumed and view.has('call_r2_write') and view.tools == ['UpdatePlan', 'Write']


def stream(plan):
    """The plan's bytes as the provider writes them (pauses and holds are only time)."""
    return b''.join(v.sse_bytes(kind, value) for kind, value in plan.parts if kind in ('event', 'comment'))


def parse(plan):
    """The plan's stream read by Pebble's own Responses stream parser (one pass, no network)."""
    from pebble.recovery import StreamBudget
    from pebble.responses import _stream_pass

    class Provider:
        watched_open, connect_observer, response = False, None, None

        @staticmethod
        def headers(kind, key):
            return {}

        @staticmethod
        def request(url, headers, body, timeout=None):
            return io.BytesIO(stream(plan))
    text, reasoning, timing = [], [], {}
    state = _stream_pass(Provider, {'url': 'http://127.0.0.1:9/v1'}, {}, '', threading.Event(), text.append,
                         reasoning.append, StreamBudget(5.0, 10.0), timing, False)
    return state, ''.join(text), ''.join(reasoning)


def test_the_scripted_streams_are_read_by_pebbles_own_responses_parser():
    from pebble.stats import attempt_report, usage_metrics
    campaign = stub()
    scripts = campaign.provider.scripts
    state, text, reasoning = parse(scripts['r1'](v.View('r1', 0, body('x'))))
    assert state.completed and text == v.REPLIES['r1'] and reasoning == ''.join(v.R1_SUMMARY)
    report = attempt_report(usage_metrics(state.usage, {}, None, None))
    assert report['input'] == 400 and report['output'] == 20
    state, text, _ = parse(scripts['u1'](v.View('u1', 0, body('x'))))
    report = attempt_report(usage_metrics(state.usage, {}, None, None))
    assert (report['input'], report['cached'], report['output'], report['reasoning']) == (1234, 1000, 56, 12)
    write = scripts['r2'](v.View('r2', 0, body('x')))
    state, text, _ = parse(write)
    calls = [i for i in state.streamed.output(state.terminal) if i['type'] == 'function_call']
    assert write.kind == 'write' and text == '' and [c['name'] for c in calls] == ['Write']
    assert v.WRITE_MARK in json.loads(calls[0]['arguments'])['content']
    held = scripts['r2'](v.View('r2', 1, body('x', outputs=['call_r2_write'])))
    after = scripts['r2'](v.View('r2', 2, body('x', outputs=['call_r2_write'], host=['HOST RESUME: Continue.'])))
    assert (held.kind, after.kind) == ('held_draft', 'after_decision')
    assert parse(held)[2] == v.HELD_MARK and parse(after)[2] == v.SHOWN_MARK and v.DRAFT_MARK in parse(held)[1]
    # The SSE comments of i2 are liveness only, and the reply after them completes.
    comments = scripts['i2'](v.View('i2', 0, body('x')))
    assert [k for k, _ in comments.parts].count('comment') == v.COMMENTS and parse(comments)[0].completed
    # i1 streams text and a finished call, then holds: read to its end, the attempt never completes.
    quiet = scripts['i1'](v.View('i1', 0, body('x')))
    state, text, _ = parse(quiet)
    assert quiet.parts[-1][0] == 'hold' and not state.completed and text == v.PARTIAL
    assert [i['name'] for i in state.streamed.done.values()] == ['UpdatePlan']
    assert scripts['i1'](v.View('i1', 1, body('x'))).kind == 'reply'
    assert scripts['w1'](v.View('w1', 0, body('x'))).parts == [('hold', v.HOLD_STALL)]
    assert scripts['w1'](v.View('w1', 1, body('x'))).kind == 'reply'
    # s4 writes into ~/.pebble, then into the old app folder, then reports.
    first = scripts['s4'](v.View('s4', 0, body('x')))
    second = scripts['s4'](v.View('s4', 1, body('x', outputs=['call_s4_settings'])))
    paths = [json.loads(c['arguments'])['file_path'] for p in (first, second)
             for c in parse(p)[0].streamed.output(parse(p)[0].terminal)]
    assert paths == ['home/.pebble/settings.json', 'home/Library/Application Support/com.pebble.local/MOVED.md']
    assert parse(scripts['s4'](v.View('s4', 2, body('x', outputs=['call_s4_settings', 'call_s4_moved']))))[1] == \
        v.REPLIES['s4']
    # The long summary is above run_activity's cap and starts with its marker.
    assert sum(map(len, v.LONG_PARTS)) > v.REASONING_CAP and v.LONG_PARTS[0].startswith(v.LONG_MARK)


def test_the_provider_records_requests_content_free_and_flags_other_routes():
    provider = stub().provider
    plan = provider.respond(body('Reply ' + v.MARK + 'u1', outputs=['call_x']), 'main')
    assert plan.kind == 'reply'
    record = provider.records[-1]
    assert record['scenario'] == 'u1' and record['outputs'] == ['call_x'] and record['store'] is False
    assert 'Reply' not in json.dumps(record) and provider.outputs[('u1', 'call_x')]
    provider.respond(body('No tag here'), 'main')
    assert provider.anomalies and provider.anomalies[-1]['reason'] == 'unscripted request'
    for bad in ({**body('x'), 'model': 'another-model'},
                {**body('x'), 'input': [{'role': 'user', 'content': 'x', '_pebble_current': True}]}):
        with pytest.raises(AssertionError):
            provider.respond(bad, 'main')


# The black hole -------------------------------------------------------------------------------------------------------

def test_localhost_has_both_families_and_a_bound_silent_port_drops_connections_on_each():
    """w2's black hole on this machine: a loopback port bound but not listening answers nothing (XNU drops the SYN
    without a reset), on both families `localhost` resolves to; once it listens, it answers."""
    addresses = v.localhost_addresses()
    assert len(addresses) >= 2 and {f for f, _ in addresses} == {socket.AF_INET, socket.AF_INET6}, addresses
    for family, address in addresses:
        port = v.SilentPort(family, address)
        try:
            assert port.probe() == 'dropped', (address, port.probe())
            port.sock.listen(1)
            assert port.probe() == 'answered', address
        finally:
            port.close()


# The vault helper and Pebble for Chrome (their fakes) -----------------------------------------------------------------

def test_the_vault_checks_pass_against_the_fake_helper_process(tmp_path):
    campaign = stub()
    campaign.scenario('vault', campaign.scenario_vault, tmp_path / 'vault')
    assert not failures(campaign, v.SCENARIOS['vault']), (failures(campaign, v.SCENARIOS['vault']),
                                                          campaign.observations.get('scenario_errors'))
    evidence = results(campaign)['k1_the_vault_helper_protocol_is_ping_get_all_and_put_all_byte_exact']['evidence']
    assert evidence['old_item_reads'] == 1 and evidence['ops']['put_all'] >= 3
    secret, old = campaign.facts['vault_secrets']
    assert secret not in json.dumps(campaign.checks) and old not in json.dumps(campaign.checks)


def test_the_vault_helper_launcher_runs_an_interpreter_path_with_spaces(tmp_path):
    from pebble import vault
    launcher, log, _ = v.write_helper(tmp_path / 'with space', 'ok')
    vault.VaultHelper(launcher).ping()
    assert v.helper_log(log)[0]['op'] == 'ping' and v.helper_log(log)[0]['exact'] is True


# Phase 22 finding (v22-validator): when Pebble ends the relay while Chrome holds the host's stdin, the host's upstream
# daemon thread is blocked inside sys.stdin.buffer.read, and CPython aborts at shutdown (SIGABRT, "Fatal Python error:
# _enter_buffered_busy: could not acquire lock for <_io.BufferedReader name='<stdin>'>"). The proposed patch (track
# note) reads Chrome's stdin through the unbuffered raw file, which holds no lock.
NATIVE_HOST = ROOT / 'backend/pebble/native_host.py'
STDIN_BUFFERED = 'stdin = stdin if stdin is not None else sys.stdin.buffer\n'
STDIN_RAW = 'stdin = stdin if stdin is not None else sys.stdin.buffer.raw'
STDIN_LOCK = "could not acquire lock for <_io.BufferedReader name='<stdin>'>"
B6 = 'b6_the_native_host_exits_cleanly_when_pebble_ends_the_relay'


def host_patched():
    return STDIN_RAW in NATIVE_HOST.read_text(encoding='utf-8')


# Phase 22 finding (v22-validator): in a ~/.pebble home every staged Write, Edit or Append fails. guard.Guard._stage roots
# a policy.Workspace at the AgentGuard mirror <data>/guard-projects/<id>, and the data folder is protected; the merge's
# data_home.WORK_AREAS exempts git-worktrees and environments only. Flat --home tests cannot see it. The proposed patch
# (track note) adds 'guard-projects' to WORK_AREAS. r2 (whose Write must be staged to hold its draft) and s5 report it.
WORK_AREAS_LINE = "WORK_AREAS = ('git-worktrees', 'environments')"
S5 = 's5_an_ordinary_project_write_is_still_staged_in_the_pebble_home'
R2 = 'r2_an_approval_hold_holds_the_summary_with_its_draft'


def guard_patched():
    from pebble import data_home
    return 'guard-projects' in data_home.WORK_AREAS


def known_failures():
    """The checks the two unpatched defects fail, in planned order (none once both patches are applied)."""
    known = set() if host_patched() else {B6}
    known |= set() if guard_patched() else {R2, S5}
    return [name for name in v.PLANNED if name in known]


def test_the_proposed_work_area_patch_lets_the_guard_mirror_stage_edits_in_a_pebble_home(tmp_path, monkeypatch):
    """What guard.Guard._stage does with every staged edit, in a ~/.pebble home: the mirror is refused today; with
    'guard-projects' as a work area it is allowed, while ~/.pebble, its data folder and the area itself stay refused."""
    from pebble import data_home
    from pebble.policy import PolicyError, Workspace
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    root = tmp_path / 'home' / '.pebble'
    data = root / 'data'
    mirror = data / 'guard-projects' / ('a' * 24)
    mirror.mkdir(parents=True)
    data_home.activate(data_home.Layout(root, True, 'default'), data, {})
    try:
        if not guard_patched():
            with pytest.raises(PolicyError, match="Pebble's own data folders"):
                Workspace(mirror).path('notes.md', create=True)
            assert WORK_AREAS_LINE in (ROOT / 'backend/pebble/data_home.py').read_text(encoding='utf-8')
        monkeypatch.setattr(data_home, 'WORK_AREAS', tuple(dict.fromkeys(data_home.WORK_AREAS + ('guard-projects',))))
        assert Workspace(mirror).path('notes.md', create=True) == mirror / 'notes.md'
        for refused in (root, data, data / 'guard-projects'):
            with pytest.raises(PolicyError, match="Pebble's own data folders"):
                Workspace(refused)
    finally:
        data_home.deactivate(data)


def test_the_browser_checks_pass_with_the_source_backend_as_the_native_host(tmp_path):
    campaign = stub(command=(sys.executable, str(ROOT / 'backend/main.py')))
    try:
        campaign.scenario('browser', campaign.scenario_browser, tmp_path / 'browser')
    finally:
        for path in campaign.cleanup:
            shutil.rmtree(path, ignore_errors=True)
    others = [name for name in v.SCENARIOS['browser'] if name != B6]
    assert not failures(campaign, others), (failures(campaign, others), campaign.observations.get('scenario_errors'))
    cases = results(campaign)['b1_the_native_host_caps_frames_at_1_mib_both_ways_and_ends_on_any_bad_message']
    assert set(cases['evidence']['cases']) == {'relay', 'oversized_from_extension', 'oversized_from_bridge',
                                               'second_hello', 'not_json', 'oversized_hello', 'wrong_origin', 'no_app',
                                               'pebble_quits'}
    clean = results(campaign)[B6]
    if host_patched():
        assert clean['passed'], clean.get('error')
    else:  # b6 reports the defect exactly: the two runs Pebble ended, by the stdin lock at shutdown, and no other
        assert not clean['passed'] and STDIN_LOCK in clean['error'], clean.get('error')
        crashed = {name for name in ('oversized_from_bridge', 'pebble_quits') if f"'{name}': (-6" in clean['error']}
        assert crashed == {'oversized_from_bridge', 'pebble_quits'} and clean['error'].count('(-6,') == 2, clean['error']


def run_quitting_pebble(tmp_path, name, source):
    """One 'Pebble quits while Chrome holds the port' run of a native_host module written from `source`."""
    from pebble import native_host
    folder = tmp_path / name
    folder.mkdir()
    (folder / 'native_host_copy.py').write_text(source, encoding='utf-8')
    launcher = folder / 'host.py'
    launcher.write_text(f'import sys\nsys.path.insert(0, {str(folder)!r})\nimport native_host_copy\n'
                        'sys.exit(native_host_copy.main(sys.argv[2:]))\n', encoding='utf-8')
    home, short = v.short_folder(folder, 'h')
    bridge = v.FakeBridge(home, 'close')
    host = v.NativeHost([sys.executable, '-I', str(launcher)], home, native_host.EXTENSION_ORIGIN,
                        {'PATH': '/usr/bin:/bin', 'HOME': str(folder)}, folder, folder / 'stderr')
    try:
        host.write(v.frame({'type': 'hello', 'protocol': native_host.PROTOCOL, 'nonce': 'ab' * 32}))
        assert host.frame(0) and host.frame(0)['type'] == 'welcome'
        return host.finish(), host.stderr_head()
    finally:
        host.finish(seconds=2)
        bridge.close()
        if short:
            shutil.rmtree(home, ignore_errors=True)


def test_the_proposed_native_host_patch_exits_cleanly_when_pebble_quits(tmp_path):
    source = NATIVE_HOST.read_text(encoding='utf-8')
    buffered = source.replace(STDIN_RAW, STDIN_BUFFERED.rstrip('\n')) if host_patched() else source
    assert '    ' + STDIN_BUFFERED in buffered, 'native_host.main no longer has the anchor the patch replaces'
    code, stderr = run_quitting_pebble(tmp_path, 'buffered', buffered)
    assert code == -6 and STDIN_LOCK in stderr, (code, stderr)  # the defect, reproduced on the unpatched module
    patched = buffered.replace(STDIN_BUFFERED, STDIN_RAW + '\n', 1)
    code, stderr = run_quitting_pebble(tmp_path, 'raw', patched)
    assert code == 0 and stderr == '', (code, stderr)


# File, database and Markdown helpers ----------------------------------------------------------------------------------

def test_tree_tables_and_hits_read_without_following_links(tmp_path):
    folder = tmp_path / 'home'
    (folder / 'sub').mkdir(parents=True)
    (folder / 'sub' / 'a.txt').write_text('alpha marker-1\n')
    (folder / 'runtime-owner.lock').write_text('lock')
    (folder / 'pebble.sqlite-wal').write_text('wal')
    (folder / 'link').symlink_to(tmp_path)
    with sqlite3.connect(folder / v.DATABASE) as db:
        db.execute('CREATE TABLE notes(text TEXT)')
        db.execute("INSERT INTO notes VALUES ('beta marker-2')")
        db.execute('PRAGMA user_version=9')
    db.close()
    assert set(v.tree(folder)) == {'sub/a.txt', 'link'} and v.tree(folder)['link'].startswith('link:')
    found, version = v.tables(folder / v.DATABASE)
    assert version == 9 and found['notes']['rows'] == 1
    assert v.database_hits(folder / v.DATABASE, ['marker-2', 'absent'], live=False) == {'marker-2': ['notes']}
    hits, files, _ = v.file_hits([folder], ['marker-1', 'absent'])
    assert set(hits) == {'marker-1'} and files >= 3


def test_memory_and_conversation_copies_are_edited_as_a_person_would(tmp_path):
    from pebble.data_mirror import _memory_text
    rows = [{'id': f'm{i}', 'content': f'fact {i}', 'scope': 'global', 'origin': 'user', 'updated': 0, 'expires': None}
            for i in (1, 2)]
    text = _memory_text('semantic', 'Facts & knowledge', rows, 2, False)
    removed = v.remove_memory_block(text, 'm1')
    assert 'fact 1' not in removed and 'pebble:memory id=m1' not in removed and 'pebble:memory id=m2' in removed
    edited = v.edit_memory_block(text, 'm2', 'changed')
    assert 'fact 2' not in edited and 'changed' in edited and 'pebble:memory id=m2' in edited and 'fact 1' in edited
    (tmp_path / '2026-10-14 Chat.md').write_text('<!-- pebble:conversation id=' + 'a' * 32 + ' -->\n# Chat\n')
    (tmp_path / 'notes.md').write_text('# not a copy\n')
    assert v.conversation_files(tmp_path) == {'a' * 32: tmp_path / '2026-10-14 Chat.md'}
    near, short = v.short_folder(tmp_path, 'x')
    try:
        assert near.is_dir() and len(str(near / 'chrome/bridge.sock')) <= 103
        assert short is (len(str(tmp_path / 'x')) + len('/chrome/bridge.sock') > 100)
    finally:
        if short:
            shutil.rmtree(near, ignore_errors=True)


# The command line ---------------------------------------------------------------------------------------------------

# The scenarios the small campaign replaces with a no-op; static and legacy run as written, on one real service.
REPLACED = {'stall_start': 'scenario_stall_start', 'vault': 'scenario_vault', 'browser': 'scenario_browser',
            'migration': 'scenario_migration', 'stream': 'scenario_stream', 'reasoning': 'scenario_reasoning',
            'activity': 'scenario_activity', 'documents': 'scenario_documents', 'settings': 'scenario_settings',
            'stall_result': 'scenario_stall_result', 'restart': 'scenario_restart'}


@pytest.mark.parametrize('frozen', [False, True], ids=['source', 'backend'])
def test_the_command_line_runs_a_small_campaign_and_binds_the_backend_it_launched(tmp_path, monkeypatch, frozen):
    """main() and the real run_all on the static checks and the legacy home of one real service; every other scenario
    is a no-op, so its checks are recorded "Not run" and the report fails. With --backend (an owned launcher around the
    source backend: the plumbing, not a frozen binary) the service starts through it and the report binds it."""
    import pebble
    # Phase 23 (the person's decision, 2026-10-14): validate_phase22 is retired because RELEASE pins 0.6.0; the checkout
    # is now later, so its release checks are run against the current release here (validate_phase23 does the same).
    monkeypatch.setattr(v, 'RELEASE', pebble.VERSION)
    assert set(REPLACED) == set(v.SCENARIOS) - {'static', 'legacy'}
    for method in REPLACED.values():
        monkeypatch.setattr(v.Campaign, method, lambda self, *args: None)
    temporary = tmp_path / 'tmp'
    temporary.mkdir()
    monkeypatch.setattr(v.tempfile, 'tempdir', str(temporary))  # main()'s scratch, outside the checkout
    report_path, launcher, marker = tmp_path / 'report.json', tmp_path / 'pebble-backend', tmp_path / 'launched'
    argv = ['--report', str(report_path)]
    if frozen:
        launcher.write_text('#!/bin/sh\necho launched > ' + json.dumps(str(marker)) + '\nexec ' + json.dumps(sys.executable)
                            + ' ' + json.dumps(str(ROOT / 'backend/main.py')) + ' "$@"\n')
        launcher.chmod(0o755)
        argv += ['--backend', str(launcher)]
    assert v.main(argv) == 1  # a campaign that ran only part of its scenarios never passes
    assert (report_path.stat().st_mode & 0o777) == 0o600
    report = json.loads(report_path.read_text())
    found = {c['name']: c for c in report['check_results']}
    assert report['kind'] == ('frozen' if frozen else 'source') and report['passed'] is False
    assert report['backend_sha256'] == (v.sha(launcher.read_bytes()) if frozen else None)
    assert report['backend_path'] == (str(launcher.resolve()) if frozen else None)
    assert marker.is_file() is frozen  # the service started through the launcher, and only with --backend
    assert report['all_planned_checks_recorded'] and list(found) == list(v.PLANNED)
    ran = [*v.COMMON_FIRST, *v.SCENARIOS['static'], *v.SCENARIOS['legacy'], *v.COMMON_LAST]
    assert report['checks'] == ran, {name: found[name].get('error') for name in ran if not found[name]['passed']}
    assert all(found[name].get('not_run') and not found[name]['passed']
               for scenario in REPLACED for name in v.SCENARIOS[scenario])
    services = report['observations']['services']
    assert [(s['label'], s['exit_code']) for s in services] == [('legacy', 0)] and services[0]['startup_seconds'] > 0
    assert report['synthetic_provider_attempts'] >= 2 and report['fatal_error'] is None
    assert all('Synthetic' not in json.dumps(r) and 'harbour' not in json.dumps(r) for r in report['request_metadata'])
    assert set(report['source_sha256']) == set(v.checkout_digests())
    scratch = Path(report['scratch_preserved'])  # kept because the report failed
    legacy = scratch.joinpath('person', 'home', *v.LEGACY_PARTS)
    assert scratch.parent == temporary and (legacy / v.DATABASE).is_file()
    assert not (scratch / 'person' / 'home' / '.pebble').exists()  # a flat --home is never a Pebble home


def test_the_whole_campaign_passes_against_the_source_service(tmp_path, monkeypatch):
    """main() and every scenario against the source backend (about 65 s: the 60 s loopback stall runs in the background
    while the other scenarios run). Until the two proposed patches land, exactly b6, r2 and s5 fail, for the defects
    they name (known_failures); the track note records the same campaign passing on a backend copy with both patches.
    The verifier runs the same campaign in source and frozen modes."""
    import pebble
    # Phase 23: validate_phase22 is retired (RELEASE pins 0.6.0); its campaign runs here against the current release.
    monkeypatch.setattr(v, 'RELEASE', pebble.VERSION)
    temporary = tmp_path / 'tmp'
    temporary.mkdir()
    monkeypatch.setattr(v.tempfile, 'tempdir', str(temporary))
    report_path = tmp_path / 'report.json'
    code = v.main(['--report', str(report_path)])
    report = json.loads(report_path.read_text())
    found = {c['name']: c for c in report['check_results']}
    known = known_failures()  # until the two patches land, b6, r2 and s5 report their defects
    assert report['failed'] == known and report['fatal_error'] is None, json.dumps(
        {'failed': {name: found[name].get('error') for name in report['failed']}, 'fatal': report['fatal_error'],
         'scenario_errors': report['observations'].get('scenario_errors'),
         'run_errors': report['observations'].get('run_errors'), 'stderr': report['backend_stderr_tail']})[:12000]
    assert code == (1 if known else 0) and report['passed'] is (not known)
    assert report['checks'] == [n for n in v.PLANNED if n not in known] and report['duration_seconds'] < v.BUDGET_SECONDS
    if B6 in known:
        assert STDIN_LOCK in found[B6]['error']
    if S5 in known:  # the errors are tuple reprs, so the apostrophe in "Pebble's" arrives escaped
        assert 'own data folders' in found[S5]['error'] and 'own data folders' in found[R2]['error']
    if known:
        shutil.rmtree(report['scratch_preserved'], ignore_errors=True)  # kept by main() because the report failed
    assert not list(temporary.iterdir())
    labels = [s['label'] for s in report['observations']['services']]
    assert all(s['exit_code'] == 0 for s in report['observations']['services']), report['observations']['services']
    assert {'stall', 'legacy', 'move-stopped', 'main', 'env', 'restart'} <= set(labels)
    text = json.dumps(report)
    assert not any(mark in text for mark in v.SUMMARY_MARKS)  # the report itself never carries summary text
