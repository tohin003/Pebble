"""Phase 35: scripts/validate_phase35.py, 1.3.0's cache-first requests and parallel tool calls (source and frozen).

The real checkout passes; each check fails when the defect it guards returns (the checkpoint rewritten each step, the
audit inserted near the top again, the time back in the system prompt, a tool joining mid-run without a page, parallel
calls at 8K, notes that name Context Warping or claim a saving), so no check passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase35_under_test', ROOT / 'scripts/validate_phase35.py')
v35 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase35_under_test'] = v35
spec.loader.exec_module(v35)


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v35.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def edited(monkeypatch, rel, old, new):
    real = v35.read

    def read(name):
        text = real(name)
        if name == rel:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v35, 'read', read)


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v35.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items() if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v35.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v35.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v35.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v35_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v35.READS) <= set(vhc.sources()) and vhc.SUITES['phase35'] == 'validate_phase35.py'


def test_a_checkpoint_rewritten_each_step_fails(tmp_path, monkeypatch):
    from pebble import runner
    real = runner.prefix_report

    def rewritten(state, messages, tools):
        changed = [dict(m) for m in messages]
        if len(changed) > 1:
            changed[1]['content'] = changed[1].get('content', '') + ' step ' + str(len(messages))
        return real(state, changed, tools)
    monkeypatch.setattr(runner, 'prefix_report', rewritten)
    check = 'requests_above_8k_extend_each_other'
    assert not campaign(tmp_path, {check})[check]['passed']


def test_the_time_back_in_the_system_prompt_fails(tmp_path, monkeypatch):
    from pebble import runner
    monkeypatch.setattr(runner, 'now_line', lambda: 'Current date: 2026-10-24. Local time: 07:00.')
    check = 'the_system_prompt_is_the_same_for_a_days_tasks'
    assert not campaign(tmp_path, {check})[check]['passed']


def test_a_tool_joining_mid_run_without_a_page_fails(tmp_path, monkeypatch):
    from pebble import runner
    from pebble.tool_catalogue import TOOLS
    real, calls = runner.with_screen_tools, []

    def staged(offered, names):
        calls.append(1)
        offered = real(offered, names)
        return offered if len(calls) == 1 else runner.in_catalogue_order(
            offered, [t for t in TOOLS if t['function']['name'] == 'ReadSource'])
    monkeypatch.setattr(runner, 'with_screen_tools', staged)
    check = 'a_run_keeps_one_tool_list_until_a_page_is_recorded'
    assert not campaign(tmp_path, {check})[check]['passed']


def test_parallel_calls_at_8k_fail(tmp_path, monkeypatch):
    from pebble import responses
    monkeypatch.setattr(responses, 'PARALLEL_ABOVE', 4096)
    check = 'chatgpt_allows_parallel_tool_calls_above_8k'
    assert not campaign(tmp_path, {check})[check]['passed']


@pytest.mark.parametrize('old, new', [
    ('extends the previous one, so prompt caching can reuse it', 'is cached, so it is 90 % cheaper'),
    ('Requests that a model provider can cache', 'Context Warping: requests that a model provider can cache'),
])
def test_notes_that_overclaim_fail(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'CHANGELOG.md', old, new)
    check = 'release_version_is_1_3_0_and_its_notes_describe_it'
    assert not campaign(tmp_path, {check})[check]['passed']
