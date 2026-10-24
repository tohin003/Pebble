"""Phase 37: scripts/validate_phase37.py, 1.5.0's Code Mode and the Stealth full-screen fix (source and frozen).

The real checkout passes; each check fails when the defect it guards returns (a script's calls without their own
receipts, a sandbox that allows the network, more printed output than promised, a tool the step does not offer made
callable, the switch ignored, the worker flag missing, the panel switched mid-exit, notes that claim a saving or name
Context Warping), so no check passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase37_under_test', ROOT / 'scripts/validate_phase37.py')
v37 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase37_under_test'] = v37
spec.loader.exec_module(v37)
macos = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox')


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v37.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def fails(tmp_path, check):
    result = campaign(tmp_path, {check})[check]
    assert not result['passed'], result.get('evidence')
    return result


def edited(monkeypatch, rel, old, new):
    real = v37.read

    def read(name):
        text = real(name)
        if name == rel:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v37, 'read', read)


@macos
def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v37.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items()
                                                        if not r['passed']}


@macos
def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v37.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v37.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v37.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v37_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v37.READS) <= set(vhc.sources()) and vhc.SUITES['phase37'] == 'validate_phase37.py'


@macos
def test_script_calls_without_their_own_receipts_fail(tmp_path, monkeypatch):
    from pebble import code_mode

    def shared(app, message, tools, offered, step_tools, ws, cid, rid, call_id, calls, receipts, Actor):
        name = code_mode.INNER.get(message.get('tool'))
        calls[name] += 1
        try:
            return {'result': app.execute(name, message.get('args') or {}, ws, cid)}
        except Exception as exc:
            return {'error': str(exc)}
    monkeypatch.setattr(code_mode, '_serve', shared)
    fails(tmp_path, 'a_script_calls_tools_and_only_its_printed_output_returns')


@macos
def test_a_sandbox_that_allows_the_network_fails(tmp_path, monkeypatch):
    from pebble import code_mode
    real = code_mode.profile
    monkeypatch.setattr(code_mode, 'profile', lambda *a, **k: real(*a, **k) + '\n      (allow network*)')
    fails(tmp_path, 'the_worker_sandbox_denies_network_project_files_processes_and_outside_writes')


@macos
def test_more_printed_output_than_promised_fails(tmp_path, monkeypatch):
    from pebble import code_mode
    monkeypatch.setattr(code_mode, 'SHOWN', 24_000)
    fails(tmp_path, 'scripts_are_bounded_in_time_and_output')


@macos
def test_a_command_made_callable_from_a_script_fails(tmp_path, monkeypatch):
    from pebble import code_mode
    monkeypatch.setattr(code_mode, 'INNER', {**code_mode.INNER, 'run_command': 'RunCommand'})
    fails(tmp_path, 'calls_from_a_script_take_the_direct_path')


@macos
def test_a_switch_that_does_not_switch_fails(tmp_path, monkeypatch):
    from pebble import code_mode
    monkeypatch.setattr(code_mode, 'enabled', lambda app: True)
    fails(tmp_path, 'runcode_is_offered_above_8k_with_a_project_from_the_first_step')


def test_a_backend_without_the_worker_flag_fails(tmp_path, monkeypatch):
    real = v37.subprocess.Popen

    def popen(command, *args, **kwargs):
        return real([c if c != '--code-worker' else '--hook-worker' for c in command], *args, **kwargs)
    monkeypatch.setattr(v37.subprocess, 'Popen', popen)
    fails(tmp_path, 'the_worker_runs_from_the_backend_executable')


def test_a_panel_switched_while_leaving_full_screen_fails(tmp_path, monkeypatch):
    edited(monkeypatch, 'src-tauri/src/main.rs',
           'if leaving { LEFT_FULL_SCREEN.store(true, Ordering::SeqCst); windowed_after_full_screen(app); }\n'
           '            else { let _=stealth_mac::set_panel(ns, on); }',
           'let _=stealth_mac::set_panel(ns, on);')
    fails(tmp_path, 'stealth_leaves_full_screen_into_an_ordinary_window')


def test_a_chatgpt_request_without_the_session_header_fails(tmp_path, monkeypatch):
    from pebble import responses
    monkeypatch.setattr(responses, 'request_headers', lambda provider, profile, key, body: provider.headers('responses', key))
    fails(tmp_path, 'chatgpt_requests_name_the_run_in_session_id')


@pytest.mark.parametrize('old, new', [
    ('no saving is claimed here', 'it is 50 % cheaper'),
    ("Pebble's Workshop, part 1: Code Mode.", "Pebble's Workshop, part 1: Code Mode, with Context Warping."),
])
def test_notes_that_overclaim_fail(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'CHANGELOG.md', old, new)
    fails(tmp_path, 'release_version_is_1_5_0_and_its_notes_describe_it')
