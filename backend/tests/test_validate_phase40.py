"""Phase 40: scripts/validate_phase40.py, 1.6.0's all-rounder core (the Companion's sandboxed general tools).

The command line passes on this checkout, and each check fails when the guarantee it holds is broken (the broken
behaviour is patched in process for one campaign run; the checkout is never edited).
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/validate_phase40.py'
pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='the Shell needs the macOS sandbox')


def load():
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('validate_phase40_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def campaign(tmp_path, monkeypatch, only):
    v40 = load()
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setenv('HOME', os.environ.get('HOME', '/tmp'))  # restored by monkeypatch after the run
    run = v40.Campaign(scratch, only={'fixture_scratch_created_outside_the_checkout', *only})
    try:
        run.run()
    finally:
        run.close()
    return {r['name']: r for r in run.results}


def fails(tmp_path, monkeypatch, name):
    results = campaign(tmp_path, monkeypatch, {name})
    assert results['fixture_scratch_created_outside_the_checkout']['passed']
    assert not results[name]['passed'], results[name]
    return results[name]['error']


def test_the_command_line_passes_on_this_checkout(tmp_path):
    report = tmp_path / 'v40.json'
    done = subprocess.run([sys.executable, str(SCRIPT), '--report', str(report)], capture_output=True, text=True,
                          cwd=ROOT, timeout=300, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    data = json.loads(report.read_text())
    assert done.returncode == 0 and data['passed'] is True, (done.stdout[-2000:], done.stderr[-2000:])
    assert data['checks'] == list(load().PLANNED) and data['kind'] == 'source'


def test_no_offer_fails_the_offer_check(tmp_path, monkeypatch):
    from pebble import runner
    monkeypatch.setattr(runner, 'companion_tools_enabled', lambda: False)
    fails(tmp_path, monkeypatch, 'the_companion_is_offered_the_general_tools_above_8k_only')


def test_a_policy_that_allows_everything_fails_the_sandbox_check(tmp_path, monkeypatch):
    from pebble import shell_policy
    real = shell_policy.decide

    def lenient(line, **kw):
        decision = real(line, **kw)
        decision.action, decision.hosts, decision.folders, decision.gates = 'allow', [], [], []
        return decision
    monkeypatch.setattr(shell_policy, 'decide', lenient)
    fails(tmp_path, monkeypatch, 'shell_runs_in_the_sandbox_and_protected_places_stay_closed')


def test_offering_always_for_deletes_fails_the_delete_check(tmp_path, monkeypatch):
    from pebble import shell_policy
    real = shell_policy.decide

    def always(line, **kw):
        decision = real(line, **kw)
        decision.always = True
        return decision
    monkeypatch.setattr(shell_policy, 'decide', always)
    fails(tmp_path, monkeypatch, 'deleting_outside_the_workbench_is_asked_every_time')


def test_ignoring_untrusted_text_and_leaks_fails_the_taint_check(tmp_path, monkeypatch):
    from pebble import companion_tools
    monkeypatch.setattr(companion_tools.State, 'leaks', lambda self, run_id, url: [])
    fails(tmp_path, monkeypatch, 'untrusted_text_cannot_drive_the_network_or_carry_files_out')


def test_no_undo_copy_fails_the_undo_check(tmp_path, monkeypatch):
    from pebble import companion_shell
    real = companion_shell.snapshot

    def no_copy(paths, store):
        record = real([], store)
        record['entries'] = [{'path': str(p), 'existed': True, 'undoable': False, 'why': 'patched'} for p in paths]
        return record
    monkeypatch.setattr(companion_shell, 'snapshot', no_copy)
    fails(tmp_path, monkeypatch, 'file_writes_keep_a_copy_that_undo_restores')


def test_a_screenshot_that_never_reaches_the_model_fails(tmp_path, monkeypatch):
    from pebble import companion_tools
    monkeypatch.setattr(companion_tools, 'take_image', lambda app, run_id: None)
    fails(tmp_path, monkeypatch, 'a_screenshot_reaches_the_model_once_and_appears_with_the_reply')


def test_handing_over_any_tool_fails_the_find_tool_check(tmp_path, monkeypatch):
    from pebble import runner
    monkeypatch.setattr(runner, 'findable', lambda app, name, ws: True)
    fails(tmp_path, monkeypatch, 'find_tool_hands_over_only_permitted_tools')


def test_never_handing_back_fails_the_hand_back_check(tmp_path, monkeypatch):
    from pebble import shell_policy
    monkeypatch.setattr(shell_policy.BlockCounter, 'blocked', lambda self: False)
    fails(tmp_path, monkeypatch, 'three_refusals_in_a_row_hand_back_to_the_person')


@pytest.mark.parametrize('needle', ['sandbox', 'Pebble Workbench', 'FindTool'])
def test_notes_without_the_change_fail_the_release_check(tmp_path, monkeypatch, needle):
    v40 = load()
    real = v40.changelog_entry
    monkeypatch.setattr(v40, 'changelog_entry', lambda version: real(version).replace(needle, '…'))
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    run = v40.Campaign(scratch, only={'release_version_is_1_6_0_and_its_notes_describe_it'})
    run.run()
    assert not {r['name']: r for r in run.results}['release_version_is_1_6_0_and_its_notes_describe_it']['passed']


def test_a_saving_claim_fails_the_release_check(tmp_path, monkeypatch):
    v40 = load()
    real = v40.changelog_entry
    monkeypatch.setattr(v40, 'changelog_entry', lambda version: real(version) + ' It is 40% faster.')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    run = v40.Campaign(scratch, only={'release_version_is_1_6_0_and_its_notes_describe_it'})
    run.run()
    assert not {r['name']: r for r in run.results}['release_version_is_1_6_0_and_its_notes_describe_it']['passed']
