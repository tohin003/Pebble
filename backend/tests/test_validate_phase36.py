"""Phase 36: scripts/validate_phase36.py, 1.4.0's long-task sealing (source and frozen).

The real checkout passes; each check fails when the defect it guards returns (a chunk too small to matter, a chunk
that does not repay its fresh read, ReadRunEvidence joining apart from a seal, an archive that differs from the result,
a test or failed result sealed, a newest result sealed, a switch that does not switch, a journal that accepts a stale
generation, notes that name Context Warping or claim a saving), so no check passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase36_under_test', ROOT / 'scripts/validate_phase36.py')
v36 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase36_under_test'] = v36
spec.loader.exec_module(v36)


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v36.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def fails(tmp_path, check):
    result = campaign(tmp_path, {check})[check]
    assert not result['passed'], result.get('evidence')
    return result


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v36.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items()
                                                        if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v36.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v36.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v36.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v36_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v36.READS) <= set(vhc.sources()) and vhc.SUITES['phase36'] == 'validate_phase36.py'


def test_a_chunk_of_one_result_fails(tmp_path, monkeypatch):
    from pebble import context_seal
    real = context_seal.plan

    def one(messages, budget):
        chosen = real(messages, budget)
        if chosen:
            first = chosen['indices'][0]
            chosen = {**chosen, 'indices': [first], 'chars': len(messages[first]['content'])}
        return chosen
    monkeypatch.setattr(context_seal, 'plan', one)
    fails(tmp_path, 'a_long_task_seals_older_results_in_chunks')


def test_a_chunk_that_does_not_repay_its_fresh_read_fails(tmp_path, monkeypatch):
    from pebble import context_seal
    monkeypatch.setattr(context_seal, 'KEEP_MIN_EXCHANGES', 8)
    monkeypatch.setattr(context_seal, 'SEAL_PAYBACK', 0)
    fails(tmp_path, 'a_long_task_seals_older_results_in_chunks')


def test_read_run_evidence_joining_at_half_the_window_fails(tmp_path, monkeypatch):
    from pebble import runner
    from pebble.context_manifest import estimate_tokens
    real = runner.large_needs

    def eager(app, run_id, text, messages, budget, compacted=False):
        names = real(app, run_id, text, messages, budget, compacted)
        if estimate_tokens(messages) > budget // 2:
            names.add('ReadRunEvidence')
        return names
    monkeypatch.setattr(runner, 'large_needs', eager)
    fails(tmp_path, 'a_long_task_seals_older_results_in_chunks')


def test_an_archive_that_differs_from_the_result_fails(tmp_path, monkeypatch):
    from pebble import context_seal
    real = context_seal.seal

    def lossy(messages, chosen, generation):
        sealed, archive, manifest = real(messages, chosen, generation)
        archive = [{**row, 'message': {**row['message'], 'content': row['message']['content'][:-2] + '}'}}
                   for row in archive]
        return sealed, archive, manifest
    monkeypatch.setattr(context_seal, 'seal', lossy)
    fails(tmp_path, 'a_sealed_result_reads_back_byte_for_byte')


def test_a_sealed_test_or_failed_result_fails(tmp_path, monkeypatch):
    from pebble import context_seal
    monkeypatch.setattr(context_seal, '_sealable', lambda message, name: isinstance(message.get('content'), str)
                        and not message['content'].startswith('{"sealed"'))
    fails(tmp_path, 'calls_the_newest_exchanges_and_never_sealed_results_stay')


def test_a_sealed_newest_result_fails(tmp_path, monkeypatch):
    from pebble import context_seal
    monkeypatch.setattr(context_seal, '_kept', lambda exchanges, messages: 0)
    fails(tmp_path, 'calls_the_newest_exchanges_and_never_sealed_results_stay')


def test_a_payback_waived_below_three_quarters_fails(tmp_path, monkeypatch):
    from pebble import context_seal
    real = context_seal.plan
    monkeypatch.setattr(context_seal, 'plan', lambda messages, budget, pressing=False: real(messages, budget, True))
    fails(tmp_path, 'a_long_task_seals_older_results_in_chunks')


def test_a_switch_that_does_not_switch_fails(tmp_path, monkeypatch):
    from pebble import context_seal
    monkeypatch.setattr(context_seal, 'sealing', lambda app, budget: type(budget) is int and budget > 32768)
    fails(tmp_path, 'the_switch_off_keeps_the_1_3_0_requests')


def test_sealing_at_32k_fails(tmp_path, monkeypatch):
    from pebble import context_seal
    monkeypatch.setattr(context_seal, 'SEAL_FROM_BUDGET', 16384)
    monkeypatch.setattr(context_seal, 'SEAL_MIN_TOKENS', 2000)
    monkeypatch.setattr(context_seal, 'SEAL_PAYBACK', 0)
    fails(tmp_path, 'short_tasks_and_32k_windows_never_seal')


def test_a_journal_that_accepts_a_stale_generation_fails(tmp_path, monkeypatch):
    from pebble.runtime import RunJournal
    real = RunJournal.seal

    def lax(self, rid, archive, manifest, generation):
        return real(self, rid, archive, manifest, self.next_generation(rid))
    monkeypatch.setattr(RunJournal, 'seal', lax)
    fails(tmp_path, 'the_journal_refuses_a_stale_seal')


@pytest.mark.parametrize('old, new', [
    # The 1.4.0 entry's own sentence (the 1.5.0 entry, above it, repeats the closing words; Phase 37).
    ('without losing quality, is\nmeasured in the next benchmark; no saving is claimed here.',
     'without losing quality, is\nmeasured: it is 40 % cheaper.'),
    ('Long tasks keep a smaller window', 'Context Warping: long tasks keep a smaller window'),
])
def test_notes_that_overclaim_fail(tmp_path, monkeypatch, old, new):
    real = v36.read

    def read(name):
        text = real(name)
        if name == 'CHANGELOG.md':
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v36, 'read', read)
    fails(tmp_path, 'release_version_is_1_4_0_and_its_notes_describe_it')
