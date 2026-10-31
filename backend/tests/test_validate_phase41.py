"""Phase 41: scripts/validate_phase41.py, 1.7.0's Pebble's Workshop part 2 (agent-made tools and Pebble's Toolbox).

The command line passes on this checkout, and each check fails when the guarantee it holds is broken (patched in
process for one campaign run; the checkout is never edited).
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/validate_phase41.py'
pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='the worker needs the macOS sandbox')


def load():
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('validate_phase41_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def campaign(tmp_path, monkeypatch, only):
    v41 = load()
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setenv('HOME', os.environ.get('HOME', '/tmp'))
    run = v41.Campaign(scratch, only={'fixture_scratch_created_outside_the_checkout', *only})
    try:
        run.run()
    finally:
        run.close()
    return {r['name']: r for r in run.results}


def fails(tmp_path, monkeypatch, *names):
    results = campaign(tmp_path, monkeypatch, set(names))
    assert not results[names[-1]]['passed'], results[names[-1]]


def test_the_command_line_passes_on_this_checkout(tmp_path):
    report = tmp_path / 'v41.json'
    done = subprocess.run([sys.executable, str(SCRIPT), '--report', str(report)], capture_output=True, text=True,
                          cwd=ROOT, timeout=300, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    data = json.loads(report.read_text())
    assert done.returncode == 0 and data['passed'] is True, (done.stdout[-2000:], done.stderr[-2000:])
    assert data['checks'] == list(load().PLANNED)


def test_a_verdict_that_accepts_anything_fails_the_broken_tool_check(tmp_path, monkeypatch):
    from pebble import workshop
    monkeypatch.setattr(workshop, 'verdict', lambda report, tests: (True, []))
    fails(tmp_path, monkeypatch, 'a_tool_that_generated_inputs_break_is_refused')


def test_no_offer_without_a_project_fails_the_make_check(tmp_path, monkeypatch):
    from pebble import code_mode
    monkeypatch.setattr(code_mode, 'enabled', lambda app: False)
    fails(tmp_path, monkeypatch, 'a_tool_is_made_validated_and_used_in_one_script')


def test_a_keep_that_always_asks_fails_the_keep_check(tmp_path, monkeypatch):
    from pebble import workshop
    monkeypatch.setattr(workshop, 'blind_check', lambda app, spec, validate, complete=None: None)
    fails(tmp_path, monkeypatch, 'pebble_keeps_a_clean_tool_on_its_own_and_records_it')


def test_ignoring_taint_fails_the_untrusted_check(tmp_path, monkeypatch):
    from pebble import workshop
    monkeypatch.setattr(workshop, 'tainted', lambda receipts: None)
    fails(tmp_path, monkeypatch, 'a_keep_after_untrusted_output_waits_for_the_person')


def test_running_a_changed_tool_fails_the_supply_chain_check(tmp_path, monkeypatch):
    from pebble import workshop
    monkeypatch.setattr(workshop, 'verified', lambda record: True)
    fails(tmp_path, monkeypatch, 'pebble_keeps_a_clean_tool_on_its_own_and_records_it',
          'kept_tools_are_re_validated_and_a_changed_one_never_runs')


def test_a_toolbox_find_tool_ignores_fails_the_find_check(tmp_path, monkeypatch):
    from pebble import workshop
    monkeypatch.setattr(workshop, 'search', lambda app, query, limit=3: [])
    fails(tmp_path, monkeypatch, 'pebble_keeps_a_clean_tool_on_its_own_and_records_it', 'find_tool_finds_a_kept_tool')


@pytest.mark.parametrize('needle', ["Pebble's Toolbox", 'always tells you', 'View code'])
def test_notes_without_the_change_fail_the_release_check(tmp_path, monkeypatch, needle):
    v41 = load()
    real = v41.changelog_entry
    monkeypatch.setattr(v41, 'changelog_entry', lambda version: real(version).replace(needle, '…'))
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    run = v41.Campaign(scratch, only={'release_version_is_1_7_0_and_its_notes_describe_it'})
    run.run()
    assert not {r['name']: r for r in run.results}['release_version_is_1_7_0_and_its_notes_describe_it']['passed']
