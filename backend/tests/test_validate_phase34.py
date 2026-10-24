"""Phase 34: scripts/validate_phase34.py, 1.2.0's precision release (source and frozen, the same names).

The real checkout passes; each check fails on a fixture that restores the defect it guards (a check labelled as a test
run, the card without its label, no hold on an unrequested test edit, the writing rules missing or naming a corpus 24a
task, fixture or grader term, the retry fix removed, notes without the check), so no check passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase34_under_test', ROOT / 'scripts/validate_phase34.py')
v34 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase34_under_test'] = v34
spec.loader.exec_module(v34)


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v34.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def edited(monkeypatch, rel, old, new):
    real = v34.read

    def read(name):
        text = real(name)
        if name == rel:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v34, 'read', read)


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v34.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items() if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v34.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v34.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v34.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v34_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v34.READS) <= set(vhc.sources()) and vhc.SUITES['phase34'] == 'validate_phase34.py'


@pytest.mark.parametrize('check, rel, old, new', [
    ('a_check_runs_in_the_isolated_copy_and_is_never_evidence', 'src/App.vue',
     '? "Run an ad hoc check" : "Run isolated tests"', '? "Run isolated tests" : "Run isolated tests"'),
    ('an_unrequested_test_change_waits_for_the_person', 'src/App.vue',
     'typeof p.scope_review === "string"', 'false'),
    ('the_fixes_are_general', 'backend/pebble/edit_scope.py',
     'Tests are the person\'s evidence', 'Like test_quantiles.py, tests are the person\'s evidence'),
    ('release_version_is_1_2_0_and_its_notes_carry_the_precision_release', 'CHANGELOG.md',
     '(`test_pebble_check.py` or', '(a reserved name or'),
])
def test_each_check_fails_when_its_text_defect_returns(tmp_path, monkeypatch, check, rel, old, new):
    edited(monkeypatch, rel, old, new)
    assert not campaign(tmp_path, {check})[check]['passed'], check


def test_a_check_labelled_as_a_test_run_fails(tmp_path, monkeypatch):
    from pebble import testing
    real = testing.run_tests
    monkeypatch.setattr(testing, 'run_tests', lambda payload, cancel: {**real(payload, cancel), 'kind': 'test'})
    check = 'a_check_runs_in_the_isolated_copy_and_is_never_evidence'
    assert not campaign(tmp_path, {check})[check]['passed']


def test_an_edit_that_is_never_held_fails(tmp_path, monkeypatch):
    from pebble import edit_scope
    monkeypatch.setattr(edit_scope, 'review_reason', lambda path, text: None)
    check = 'an_unrequested_test_change_waits_for_the_person'
    assert not campaign(tmp_path, {check})[check]['passed']


@pytest.mark.parametrize('rules', [
    '',  # the rules removed
    '\nKeep replies short.',  # other rules in their place
    None,  # the rules naming a corpus 24a task (set below)
])
def test_missing_or_tuned_writing_rules_fail(tmp_path, monkeypatch, rules):
    from pebble import runner
    if rules is None:
        monkeypatch.setattr(runner, 'PRECISION', runner.PRECISION + ' As in c1-p75, keep the protected file.')
        check = 'the_fixes_are_general'
    else:
        monkeypatch.setattr(runner, 'PRECISION', rules)
        check = 'the_writing_rules_reach_the_model_above_8k'
    assert not campaign(tmp_path, {check})[check]['passed']


@pytest.mark.parametrize('term', ['no_distractor', 'FINAL: done', 'pages/ferry-timetable-march.md'])
def test_a_grader_term_or_fixture_in_the_test_rule_fails(tmp_path, monkeypatch, term):
    from pebble import runner
    monkeypatch.setattr(runner, 'TEST_SCOPE', runner.TEST_SCOPE + ' ' + term)
    check = 'the_fixes_are_general'
    assert not campaign(tmp_path, {check})[check]['passed']


def test_the_retry_race_returns_without_the_fix(tmp_path, monkeypatch):
    from pebble.app import App
    monkeypatch.setattr(App, '_committed_compaction', lambda self, *args: None)
    check = 'a_transport_retry_sends_the_committed_compaction'
    result = campaign(tmp_path, {check})[check]
    assert not result['passed']
    assert "'same wire'" in result['error'] or 'changed during compaction' in result['error'], result['error']
