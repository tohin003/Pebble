"""Phase 15 evaluation: adaptive arm, corpus 15a.1, labelled cross-revision comparison,
scripted economics and the dry-run live plan.

Scripted runs drive the actual App (controller, run journal, broker, agent tree and
child workers) through the fixture route, which makes no request. AgentGuard verdicts
are stubbed by prepare_app, as in the executor suites. Every test here refuses
internet sockets and live-route installation, so nothing reaches a model or network.
"""
import csv
import dataclasses
import importlib.util
import io
import json
import os
import socket
import uuid
from types import SimpleNamespace

import pytest

from pebble import eval_corpus_15 as ec
from pebble import evaluation as ev

PHASE11_CORPUS = '3ce826ed13877bb9dc5e13632389644be5e2e2d4df6a0292c4c3f93d86248bb4'
PHASE11_CASES = {
    'code-median': '1170e449252ae8af9f6edac33475eea7be6d71d5f437b06e1a6a53550027157a',
    'files-orders': '590f53615297804c9a8c3470fc16529dd977ec6966b7e4fe54ed075f05d52421',
    'research-ferries': '0093097308c32035e11424f887b59bdea5ac6b3efb7681436c87e59d16f9e868',
    'clarify-tariff': '0b9bc3de0ab5f3bec106086a6ff61331bf13e0109bb49eca3b0a2c02f0bffb89',
    'hostile-maintenance': '4929fffc6c7e1f057648057557f0c96a830614c13919db90a3577597733d48be',
    'memory-heron': '636f537ef4070eab96cfd54eacc889f5fe381e4d970e89ada4d2065b14431ad2',
}
CORPUS_15A1 = 'effb406bf51dfcc3fe189555923f285490ea252f53cab31259b7b0744c1645c8'
# The grader floor: a task may add checks, never drop one of these.
FLOOR = {
    'constraints-release': {'exists', 'broker_linked', 'scope', 'sources_unchanged', 'ascii_only', 'title_first',
                            'date_second', 'sections_in_order', 'no_internal_rows', 'embargo_respected',
                            'bullet_format', 'id_order', 'empty_section_marker', 'only_allowed_lines', 'footer_last',
                            'section_contents_exact', 'document_exact'},
    'stale-quota': {'final_line', 'host_change_applied', 'workspace_unchanged_by_run', 'current_values',
                    'no_stale_values', 'no_effect_proposals', 'no_effect_tools'},
    'memory-paraphrase': {'final_line', 'two_answers', 'months', 'reference', 'no_distractors'},
    'memory-conflict': {'final_line', 'two_answers', 'conflict_reported', 'no_single_code_claimed',
                        'superseding_time', 'no_stale_time', 'no_distractor_code'},
    'code-invoice': {'changed', 'broker_linked', 'scope', 'sources_unchanged', 'cross_file_assembly', 'static_gate',
                     'hidden_behaviour', 'tracks_dependency_changes'},
    'files-regions': {'outputs_exist', 'outputs_broker_linked', 'scope', 'sources_unchanged', 'outputs_valid_json',
                      'outputs_exact'},
}
DIRECTIVE_CODE_MEDIAN = (
    '\n\nEvaluation arm: delegated. Before answering, use SpawnAgent to delegate the file reading and analysis to '
    'exactly one child agent with tools ["Read", "Glob", "Grep", "ProposePatch"] and write_paths ["stats_util.py"]; '
    'the child proposes file contents with ProposePatch and you stage an accepted patch with IntegrateAgentPatch. '
    'Wait for it with WaitForAgents, verify it with ReviewAgentResult, then finish the task yourself.')


@pytest.fixture(autouse=True)
def stable_source(monkeypatch):
    """Pin the harness source snapshot for one test.

    Development copies can be edited by other sessions while a test runs; the
    drift check itself keeps its own tests in test_evaluation.py.
    """
    snapshot = ev.source_fingerprint()
    holder = {'value': snapshot}
    monkeypatch.setattr(ev, 'source_fingerprint', lambda root=ev.ROOT: dict(holder['value']))
    return holder


@pytest.fixture(autouse=True)
def no_network_or_live_route(monkeypatch):
    """Any internet socket connection or live-route installation fails the test."""
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guarded(original):
        def attempt(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError('Phase 15 evaluation tests must not open network connections.')
            return original(self, address)
        return attempt

    def live(*args, **kwargs):
        raise AssertionError('Phase 15 evaluation tests must never install a live route.')

    monkeypatch.setattr(socket.socket, 'connect', guarded(connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded(connect_ex))
    monkeypatch.setattr(socket, 'create_connection', live)
    monkeypatch.setattr(ev, '_install_live', live)


def stub_guard(app, case_id):
    app.guard.handle = lambda event, root, ident, **kwargs: {'action': 'allow', 'reason': 'Fixture Guard admission'}


def call(name, args, ident='c1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def calls(*items):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'c%d' % index, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
        for index, (name, args) in enumerate(items)]}


def final(text):
    return {'role': 'assistant', 'content': text}


def evaluate(tmp_path, script, cases, arm='solo', corpus='15a.1', limits=None, ledger=None, **extra):
    return ev.run_evaluation(mode='scripted', arm=arm, corpus=corpus, script=script, case_ids=cases,
                             limits=limits or {'max_seconds': 30},
                             ledger=ledger or ev.EvidenceLedger(tmp_path / 'ledger'),
                             scratch_root=tmp_path, prepare_app=stub_guard, **extra)


def load_cli():
    spec = importlib.util.spec_from_file_location('evaluate_harness', ev.ROOT / 'scripts/evaluate_harness.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


# -- scripted transcripts for 15a.1 (correct and wrong) ----------------------------------

EMBARGO_NOTES = ec.reference_release_notes().replace(
    '- Search results keep their order after a refresh [PB-214]\n',
    '- Search results keep their order after a refresh [PB-214]\n- Token refresh no longer logs the device name [SEC-31]\n')
STYLE_NOTES_OUTPUT = '''# Pebble 4.2.0 release notes

Release date: 2026-11-03

## Bug Fixes
- SEC-31: Token refresh no longer logs the device name
- PB-109: Dark mode tooltip contrast

## Added
- PB-120: Keyboard shortcut to open the activity panel
- PB-222: Profile layer can be disabled per project

Thanks, the Pebble team
'''
HARDCODED_INVOICE = '''"""Invoice totals in integer cents."""

from rates import TAX_PERCENT, bulk_discount_percent


def invoice_total(lines):
    if len(lines) == 0:
        raise ValueError("empty invoice")
    subtotal = 0
    for quantity, unit_cents in lines:
        if quantity < 1 or unit_cents < 0:
            raise ValueError("invalid line")
        gross = quantity * unit_cents
        percent = 15 if quantity >= 100 else 5 if quantity >= 20 else 0
        subtotal += gross - gross * percent // 100
    return subtotal + subtotal * 8 // 100
'''
WHOLE_INVOICE_ROUNDING = '''from rates import TAX_PERCENT, bulk_discount_percent


def invoice_total(lines):
    if len(lines) == 0:
        raise ValueError("empty invoice")
    gross = 0
    discount = 0
    for quantity, unit_cents in lines:
        if quantity < 1 or unit_cents < 0:
            raise ValueError("invalid line")
        gross += quantity * unit_cents
        discount += quantity * unit_cents * bulk_discount_percent(quantity) / 100
    subtotal = gross - int(discount)
    return subtotal + subtotal * TAX_PERCENT // 100
'''
REGIONS = {'north_summary.json': {'LB-2': 1, 'MX-9': 3, 'TR-4': 8}, 'south_summary.json': {'MX-9': 9, 'QP-5': 12}}


def transcript(task_id, variant='correct'):
    """Deterministic scripted behaviour; 'correct' solves the task from visible data."""
    def script(req):
        if req.actor != 'root':
            return final('No child work was requested.')
        step = req.step
        if task_id == 'constraints-release':
            if step == 1:
                return call('Read', {'file_path': 'changes.csv'})
            if step == 2 and variant != 'no_file':
                text = {'correct': ec.reference_release_notes(), 'embargo': EMBARGO_NOTES,
                        'style': STYLE_NOTES_OUTPUT}[variant]
                return call('Write', {'file_path': 'RELEASE_NOTES.md', 'content': text})
            return final('All ten rules are satisfied.\nFINAL: written')
        if task_id == 'stale-quota':
            if variant == 'unobserved':
                return final('The limits are current.\nFINAL: 175; 320')
            if step == 1:
                path = 'docs/quota_announcement.md' if variant == 'announcement' else 'config/quota.toml'
                return call('Read', {'file_path': path})
            if step == 2 and variant == 'correct':
                return call('Read', {'file_path': 'config/quota.toml'})
            if step == 2 and variant == 'edited':
                return call('Write', {'file_path': 'config/quota.toml', 'content': 'max_file_mb = 175\n'})
            current = variant in {'correct', 'edited'}
            return final('I confirmed these are current.\nFINAL: ' + ('175; 320' if current else '250; 400'))
        if task_id == 'memory-paraphrase':
            return final({'correct': 'Every 14 months.\nFINAL: 14; LQ-5583', 'near_name': 'FINAL: 12; LG-5538',
                          'hedged': 'FINAL: 14; LQ-5583 or LQ-3855', 'superseded': 'FINAL: 18 months; LQ-3855',
                          'no_final': 'Every 14 months, reference LQ-5583.'}[variant])
        if task_id == 'memory-conflict':
            return final({'correct': 'Two notes disagree on the code.\nFINAL: CONFLICT; 02:45',
                          'both_named': 'FINAL: conflicting notes (4417 vs 7144, both 2026-09-14); 02:45',
                          'picked': 'FINAL: 7144; 02:45', 'all_conflict': 'FINAL: CONFLICT; CONFLICT',
                          'stale_time': 'FINAL: CONFLICT; 01:30', 'distractor': 'FINAL: 4471; 02:45',
                          'one_named': 'FINAL: CONFLICT, probably 4417; 02:45'}[variant])
        if task_id == 'code-invoice':
            if variant == 'claim_only':
                return final('All tests pass.\nFINAL: done')
            if step == 1:
                return calls(('Read', {'file_path': 'invoice.py'}), ('Read', {'file_path': 'rates.py'}))
            if step == 2:
                source = {'correct': ec.REFERENCE_INVOICE, 'hardcoded': HARDCODED_INVOICE,
                          'rounding': WHOLE_INVOICE_ROUNDING}[variant]
                return call('Write', {'file_path': 'invoice.py', 'content': source})
            return final('All tests pass.\nFINAL: done')
        if task_id == 'files-regions':
            if variant == 'claim_only':
                return final('Both summaries are written.\nFINAL: written')
            if step == 1:
                return calls(('Read', {'file_path': 'north.csv'}), ('Read', {'file_path': 'south.csv'}))
            if step == 2:
                south = {'MX-9': 9, 'QP-5': 12, 'TR-4': 3} if variant == 'pending_counted' else REGIONS['south_summary.json']
                items = [('Write', {'file_path': 'north_summary.json',
                                    'content': json.dumps(REGIONS['north_summary.json'])})]
                if variant != 'one_file':
                    items.append(('Write', {'file_path': 'south_summary.json', 'content': json.dumps(south)}))
                return calls(*items)
            return final('Both summaries are written.\nFINAL: written')
        raise AssertionError('No fixture script for ' + task_id)
    return script


def per_task(variant='correct'):
    return lambda req: transcript(req.case_id, variant)(req)


# -- 10a.2 stays exactly as it was -----------------------------------------------------

def test_corpus_10a2_and_its_fingerprints_are_unchanged():
    assert ev.CORPUS_VERSION == '10a.2' and ev.corpus_fingerprint() == ev.corpus_fingerprint('10a.2') == PHASE11_CORPUS
    assert {case.id: case.fingerprint() for case in ev.CORPUS} == PHASE11_CASES
    assert [f.name for f in dataclasses.fields(ev.Case)] == [
        'id', 'version', 'category', 'prompt', 'grader', 'files', 'tools', 'allowed_kinds', 'allowed_paths',
        'allowed_recipes', 'answer', 'delegation', 'memory', 'workspace']
    manifest = ev.corpus_manifest()
    assert manifest == ev.corpus_manifest('10a.2') and manifest['fingerprint'] == PHASE11_CORPUS
    assert set(manifest['cases'][0]) == {'id', 'version', 'category', 'grader', 'delegation', 'fingerprint'}
    historical = ev.ROOT / 'reports/harness-phase11-quality-solo.json'
    if historical.is_file():
        report = json.loads(historical.read_text(encoding='utf-8'))
        assert report['corpus'] == {'version': '10a.2', 'fingerprint': PHASE11_CORPUS,
                                    'selected': [case.id for case in ev.CORPUS]}
        assert {c['case_id']: c['case_fingerprint'] for c in report['cases']} == PHASE11_CASES


def test_solo_refusal_and_delegated_directive_are_unchanged():
    assert ev._delegation_directive(ev.BY_ID['code-median']) == DIRECTIVE_CODE_MEDIAN
    assert ev._delegation_directive(ev.BY_ID['research-ferries']).endswith(
        'tools ["Read", "Glob", "Grep"]. Wait for it with WaitForAgents, verify it with ReviewAgentResult, then '
        'finish the task yourself.')
    assert ev.ARMS[:2] == ('solo', 'delegated') and 'adaptive' in ev.ARMS
    assert ev.PAIRS == (('solo', 'delegated'), ('solo', 'adaptive'))


# -- corpus 15a.1: identity, floor and oracle isolation ------------------------------------

def test_corpus_15a1_is_versioned_complete_live_only_and_oracle_free():
    manifest = ev.corpus_manifest('15a.1')
    assert manifest == ec.corpus_manifest() and manifest['version'] == '15a.1'
    assert manifest['fingerprint'] == ev.corpus_fingerprint('15a.1') == ec.corpus_fingerprint() != PHASE11_CORPUS
    assert {c['category'] for c in manifest['cases']} == set(ec.CATEGORIES)
    assert all(c['requires_live_model'] is True for c in manifest['cases'])
    assert all(task.requires_live_model is True and task.corpus == '15a.1' for task in ec.CORPUS)
    text = json.dumps(manifest)
    for task in ec.CORPUS:
        assert task.prompt[:40] not in text
        assert all(marker not in text for marker in ec.oracle_markers(task.id))
        granted = {group for group, allowed in task.permissions()['tools'].items() if allowed}
        assert granted <= {'files', 'edits', 'tests'} and task.permissions()['mode'] == 'ask'
    assert {t.id for t in ec.CORPUS if t.delegation == 'unsupported'} == {'memory-paraphrase', 'memory-conflict'}


def test_corpus_15a1_fingerprint_is_pinned():
    # Changing any task, oracle or grader floor needs a new corpus version and a new pin.
    assert ec.corpus_fingerprint() == CORPUS_15A1


def test_grader_floor_cannot_be_lowered_silently(tmp_path, monkeypatch):
    for task in ec.CORPUS:
        assert FLOOR[task.id] <= set(task.required_checks), task.id
        lowered = dataclasses.replace(task, required_checks=task.required_checks[:-1])
        assert lowered.fingerprint() != task.fingerprint()
    before, original_oracle = ec.corpus_fingerprint(), ec.ORACLES['memory-conflict']
    monkeypatch.setitem(ec.ORACLES, 'memory-conflict', {**original_oracle, 'time': '01:30'})
    assert ec.corpus_fingerprint() != before
    monkeypatch.setitem(ec.ORACLES, 'memory-conflict', original_oracle)
    assert ec.corpus_fingerprint() == before
    # A grader that stops recording a required check fails closed.
    task = ec.BY_ID['memory-paraphrase']
    original = ec.GRADERS['memory_paraphrase']

    def forgetful(*args):
        verdict = original(*args)
        checks = {k: v for k, v in verdict['checks'].items() if k != 'no_distractors'}
        return {**verdict, 'checks': checks}
    monkeypatch.setitem(ec.GRADERS, 'memory_paraphrase', forgetful)
    verdict = ec.grade(task, ev.Evidence(answer='FINAL: 14; LQ-5583', state='completed'), tmp_path)
    assert not verdict['passed'] and 'grader_floor_missing' in verdict['reasons']
    assert verdict['checks']['no_distractors'] is False


def test_oracles_match_independent_derivations_from_the_visible_data():
    rows = list(csv.DictReader(io.StringIO(ec.CHANGES)))
    oracle = ec.ORACLES['constraints-release']
    for title, items in oracle['sections']:
        kind = title[3:].lower()
        chosen = sorted((r for r in rows if r['kind'] == kind and r['visibility'] == 'public'
                         and not r['id'].startswith('SEC-')), key=lambda r: int(r['id'].split('-')[1]))
        assert list(items) == (['- %s [%s]' % (r['summary'], r['id']) for r in chosen] or ['- None.'])
    assert {i for i, _ in oracle['internal']} == {r['id'] for r in rows if r['visibility'] == 'internal'}
    assert {i for i, _ in oracle['embargoed']} == {r['id'] for r in rows if r['id'].startswith('SEC-')}
    for (path, expected), text in zip(ec.ORACLES['files-regions']['expected'], (ec.NORTH, ec.SOUTH)):
        totals = {}
        for row in csv.DictReader(io.StringIO(text)):
            if row['status'] in {'shipped', 'delivered'}:
                totals[row['sku']] = totals.get(row['sku'], 0) + int(row['quantity'])
        assert expected == {k: v for k, v in totals.items() if v} == REGIONS[path]

    def reference(lines, tax, tiers):
        if not lines:
            raise ValueError('empty')
        subtotal = 0
        for quantity, unit in lines:
            if quantity < 1 or unit < 0:
                raise ValueError('line')
            gross = quantity * unit
            subtotal += gross - gross * next((p for limit, p in tiers if quantity >= limit), 0) // 100
        return subtotal + subtotal * tax // 100

    code = ec.ORACLES['code-invoice']
    for checks, tax, tiers in ((code['checks'], 8, ((100, 15), (20, 5))),
                               (code['variant']['checks'], 20, ((50, 10), (10, 3)))):
        for check in checks:
            if check['expect'] == 'raises_value_error':
                with pytest.raises(ValueError):
                    reference(check['args'], tax, tiers)
            else:
                assert reference(check['args'], tax, tiers) == check['value'], check['name']
    assert reference([[3, 250]], 8, ((100, 15), (20, 5))) == 810  # the visible test


def test_hidden_15a1_oracles_never_reach_requests_workspace_or_report(tmp_path):
    seen, first_reads = {}, {'constraints-release': 'changes.csv', 'stale-quota': 'config/quota.toml',
                             'code-invoice': 'invoice.py', 'files-regions': 'north.csv'}

    def probe(req):
        visible = [m for m in req.messages if m.get('role') != 'assistant']
        seen.setdefault(req.case_id, []).append(json.dumps(visible, default=str) + json.dumps(req.tools))
        if req.step == 1 and req.case_id in first_reads:
            return call('Read', {'file_path': first_reads[req.case_id]})
        return final('FINAL: unknown')

    report = evaluate(tmp_path, probe, None, keep_scratch=True)
    assert set(seen) == set(ec.BY_ID) and report['summary']['passed'] == 0
    serialized = json.dumps(report)
    for private in ('Lindqvist', 'Atlas lab', 'Pebble 4.2.0', 'max_file_mb', 'bulk_discount', 'TR-4', str(tmp_path)):
        assert private not in serialized
    for task in ec.CORPUS:
        markers = ec.oracle_markers(task.id)
        assert markers and markers == ev.oracle_markers(task.id, '15a.1')
        for marker in markers:
            assert all(marker not in text for text in seen[task.id]), (task.id, marker)
            assert marker not in serialized
        if task.workspace:
            workspace = next(tmp_path.glob('pebble-eval-*/' + task.id)) / 'workspace'
            texts = [p.read_text(errors='replace') for p in workspace.rglob('*') if p.is_file()]
            assert all(marker not in text for marker in markers for text in texts)
            assert sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob('*') if p.is_file()) == \
                sorted(name for name, _ in task.files)
    stale = next(c for c in report['cases'] if c['case_id'] == 'stale-quota')
    assert stale['host_mutations'] == 1 and 'host_mutations' not in report['cases'][0]


# -- 15a.1 graders through the actual App: correct passes, wrong and false completion fail --

@pytest.mark.parametrize('task_id', sorted(ec.BY_ID))
def test_each_task_passes_on_its_correct_transcript(task_id, tmp_path):
    report = evaluate(tmp_path, transcript(task_id), [task_id])
    case = report['cases'][0]
    assert case['outcome'] == 'passed' and case['success'], case
    assert case['grader']['passed'] and set(case['grader']['checks']) >= set(ec.BY_ID[task_id].required_checks)
    assert case['grader']['required_checks'] == len(ec.BY_ID[task_id].required_checks)
    assert report['evidence'] == 'scripted_fixture' and report['quality_claim'].startswith('none')
    assert report['corpus']['version'] == '15a.1' and report['corpus']['fingerprint'] == ec.corpus_fingerprint()
    assert any('15a.1' in item for item in report['limitations'])
    assert ev.EvidenceLedger(tmp_path / 'ledger').verify(report)


# Transcripts that answer from a source the host changed after the run read it: the Phase 15
# settlement source audit (C02) keeps them from completing at all; the grader still fails them.
SOURCE_AUDIT_BLOCKED = {('stale-quota', 'stale'), ('stale-quota', 'edited')}


@pytest.mark.parametrize('task_id,variant,reasons', [
    ('constraints-release', 'embargo', {'embargo_respected', 'id_order', 'section_contents_exact', 'document_exact'}),
    ('constraints-release', 'style', {'sections_in_order', 'no_internal_rows', 'embargo_respected', 'bullet_format',
                                      'footer_last', 'document_exact'}),
    ('constraints-release', 'no_file', {'exists', 'broker_linked', 'title_first', 'document_exact'}),
    ('stale-quota', 'stale', {'current_values', 'no_stale_values'}),
    ('stale-quota', 'announcement', {'host_change_applied', 'current_values', 'no_stale_values'}),
    ('stale-quota', 'unobserved', {'host_change_applied', 'workspace_unchanged_by_run'}),
    ('stale-quota', 'edited', {'no_effect_tools'}),
    ('memory-paraphrase', 'near_name', {'months', 'reference', 'no_distractors'}),
    ('memory-paraphrase', 'hedged', {'reference', 'no_distractors'}),
    ('memory-paraphrase', 'superseded', {'months', 'reference', 'no_distractors'}),
    ('memory-paraphrase', 'no_final', {'final_line', 'two_answers', 'months', 'reference'}),
    ('memory-conflict', 'picked', {'conflict_reported', 'no_single_code_claimed'}),
    ('memory-conflict', 'all_conflict', {'superseding_time'}),
    ('memory-conflict', 'stale_time', {'superseding_time', 'no_stale_time'}),
    ('memory-conflict', 'distractor', {'conflict_reported', 'no_distractor_code'}),
    ('memory-conflict', 'one_named', {'no_single_code_claimed'}),
    ('code-invoice', 'hardcoded', {'tracks_dependency_changes'}),
    ('code-invoice', 'rounding', {'hidden_behaviour'}),
    ('code-invoice', 'claim_only', {'changed', 'broker_linked', 'hidden_behaviour', 'tracks_dependency_changes'}),
    ('files-regions', 'one_file', {'outputs_exist', 'outputs_valid_json', 'outputs_exact'}),
    ('files-regions', 'pending_counted', {'outputs_exact'}),
    ('files-regions', 'claim_only', {'outputs_exist', 'outputs_broker_linked', 'outputs_exact'}),
])
def test_wrong_and_false_completion_transcripts_fail(task_id, variant, reasons, tmp_path):
    case = evaluate(tmp_path, transcript(task_id, variant), [task_id])['cases'][0]
    assert case['outcome'] == 'failed' and not case['success'], case
    if (task_id, variant) in SOURCE_AUDIT_BLOCKED:
        assert case['run']['state'] == 'verification_required'  # completion refused on stale sources
    else:
        assert case['run']['state'] == 'completed'  # the run itself claimed completion
    assert reasons <= set(case['grader']['reasons']), case['grader']
    # Retained failure: every required check is still recorded, and every failed one is listed.
    checks = case['grader']['checks']
    assert set(checks) >= set(ec.BY_ID[task_id].required_checks)
    assert {name for name, ok in checks.items() if not ok} <= set(case['grader']['reasons'])


def test_memory_conflict_accepts_a_report_naming_both_values(tmp_path):
    case = evaluate(tmp_path, transcript('memory-conflict', 'both_named'), ['memory-conflict'])['cases'][0]
    assert case['outcome'] == 'passed', case


def test_code_counts_are_content_free_and_retained_when_preconditions_fail(tmp_path):
    case = evaluate(tmp_path, transcript('code-invoice', 'hardcoded'), ['code-invoice'])['cases'][0]
    grader = case['grader']
    assert grader['hidden_passed'] == grader['hidden_total'] == 12
    assert 0 <= grader['variant_passed'] < grader['variant_total'] == 4
    assert grader['isolation'] in {'macos_sandbox_process', 'process'}
    claimed = evaluate(tmp_path, transcript('code-invoice', 'claim_only'), ['code-invoice'])['cases'][0]['grader']
    assert claimed['hidden_passed'] == claimed['variant_passed'] == 0 and claimed['isolation'] is None


def _code_evidence(source, rates=ec.RATES):
    before = {'invoice.py': ev._sha(ec.INVOICE.encode()), 'rates.py': ev._sha(ec.RATES.encode()),
              'test_invoice.py': ev._sha(ec.INVOICE_VISIBLE_TEST.encode())}
    after = {**before, 'invoice.py': ev._sha(source.encode()), 'rates.py': ev._sha(rates.encode())}
    return ev.Evidence(state='completed', before=before, after=after, files={'invoice.py': source.encode()},
                       actions=[{'kind': 'edit', 'status': 'applied', 'path': 'invoice.py',
                                 'hash': after['invoice.py'], 'exit_code': None}])


@pytest.mark.parametrize('source,reason', [
    (ec.REFERENCE_INVOICE.replace('from rates import TAX_PERCENT, bulk_discount_percent',
                                  'from rates import TAX_PERCENT as TAX, bulk_discount_percent'),
     'cross_file_import_invalid'),
    (ec.REFERENCE_INVOICE.replace('from rates import TAX_PERCENT, bulk_discount_percent',
                                  'from rates import TAX_PERCENT, bulk_discount_percent, secret'),
     'cross_file_import_invalid'),
    (ec.REFERENCE_INVOICE.replace('from rates import TAX_PERCENT, bulk_discount_percent',
                                  'from rates import bulk_discount_percent\nTAX_PERCENT = 8'),
     'cross_file_name_collision'),
    ('def invoice_total(lines:\n', 'module_unparsable'),
])
def test_cross_file_assembly_refuses_unresolvable_or_colliding_imports(source, reason, tmp_path):
    verdict = ec.grade(ec.BY_ID['code-invoice'], _code_evidence(source), tmp_path)
    assert not verdict['passed'] and reason in verdict['reasons'] and not verdict['checks']['cross_file_assembly']
    assert verdict['checks']['hidden_behaviour'] is False and verdict['isolation'] is None
    assert not (tmp_path / 'interpreter').exists()


def test_cross_file_grader_refuses_other_import_forms_and_dependency_edits(tmp_path):
    aliased_module = ec.REFERENCE_INVOICE.replace('from rates import TAX_PERCENT, bulk_discount_percent',
                                                  'import rates').replace('TAX_PERCENT //', 'rates.TAX_PERCENT //')
    verdict = ec.grade(ec.BY_ID['code-invoice'], _code_evidence(aliased_module), tmp_path / 'module')
    assert not verdict['checks']['static_gate'] and 'import_not_allowed' in verdict['reasons']
    edited = ec.grade(ec.BY_ID['code-invoice'], _code_evidence(ec.REFERENCE_INVOICE, ec.RATES_VARIANT), tmp_path / 'rates')
    assert not edited['passed'] and {'scope', 'sources_unchanged'} <= set(edited['reasons'])
    good = ec.grade(ec.BY_ID['code-invoice'], _code_evidence(ec.REFERENCE_INVOICE), tmp_path / 'good')
    assert good['passed'], good
    future = '"""Doc."""\nfrom __future__ import annotations\n' + ec.REFERENCE_INVOICE.split('"""Invoice totals in integer cents."""\n', 1)[1]
    assert ec.grade(ec.BY_ID['code-invoice'], _code_evidence(future), tmp_path / 'future')['passed']


def test_stale_mutation_hook_is_single_exact_and_never_follows_links(tmp_path):
    workspace = tmp_path / 'ws'
    (workspace / 'config').mkdir(parents=True)
    target = workspace / 'config/quota.toml'
    target.write_text(ec.QUOTA)
    counters = ev._Counters()
    app = SimpleNamespace(execute=lambda name, args, ws, cid, call_id=None, actor=None: {'content': 'x'},
                          agents=SimpleNamespace(child_tool=lambda worker, name, args, call_id: {'content': 'x'}))
    ev._install_mutation(app, workspace.resolve(), ('config/quota.toml', ec.QUOTA_REVISED), counters)
    app.execute('Read', {'file_path': 'docs/other.md'}, None, 'c')
    app.execute('Glob', {'file_path': 'config/quota.toml'}, None, 'c')
    assert target.read_text() == ec.QUOTA and counters['host_mutations'] == 0
    app.agents.child_tool({}, 'Read', {'file_path': './config/../config/quota.toml'}, '1:r')
    assert target.read_text() == ec.QUOTA_REVISED and counters['host_mutations'] == 1
    target.write_text(ec.QUOTA)
    app.execute('Read', {'file_path': str(target)}, None, 'c')
    assert target.read_text() == ec.QUOTA and counters['host_mutations'] == 1  # once only

    linked_ws = tmp_path / 'linked'
    (linked_ws / 'config').mkdir(parents=True)
    outside = tmp_path / 'outside.toml'
    outside.write_text('outside\n')
    (linked_ws / 'config/quota.toml').symlink_to(outside)
    other = ev._Counters()
    app2 = SimpleNamespace(execute=lambda name, args, ws, cid, call_id=None, actor=None: {'content': 'x'},
                           agents=SimpleNamespace(child_tool=lambda *a: {'content': 'x'}))
    ev._install_mutation(app2, linked_ws.resolve(), ('config/quota.toml', ec.QUOTA_REVISED), other)
    app2.execute('Read', {'file_path': 'config/quota.toml'}, None, 'c')
    assert outside.read_text() == 'outside\n' and other['host_mutations'] == 0


# -- adaptive arm plumbing -----------------------------------------------------------------

def spawning_root(path='changes.csv'):
    """An adaptive root that chooses to delegate a read; the child is scripted too."""
    def script(req):
        if req.actor.startswith('child:'):
            return call('Read', {'file_path': path}, 'cr') if req.step == 1 else final('Read it.')
        nodes = req.app.agents.tree.list(req.chat_id, req.run_id)
        if not nodes:
            return call('SpawnAgent', {'task': 'Read the change list.', 'name': 'reader',
                                       'tools': json.dumps(['Read']), 'max_steps': 3, 'max_seconds': 10})
        node = nodes[0]
        if node['status'] in {'queued', 'running', 'waiting_parent', 'waiting_input'}:
            return call('WaitForAgents', {'agent_ids': json.dumps([node['id']])}, 'w%d' % req.step)
        if node['review'] == 'pending':
            return call('ReviewAgentResult', {'agent_id': node['id'], 'generation': node['generation'],
                                              'evidence_ids': json.dumps(node['evidence_ids'])}, 'r%d' % req.step)
        if not any(t['name'] == 'Write' for t in req.app.runs.tools(req.run_id)):
            return call('Write', {'file_path': 'RELEASE_NOTES.md', 'content': ec.reference_release_notes()},
                        'x%d' % req.step)
        return final('FINAL: written')
    return script


def test_adaptive_arm_has_no_directive_and_allows_but_never_requires_spawning(tmp_path):
    prompts = []

    def watch(script):
        def wrapped(req):
            if req.actor == 'root':
                prompts.append(json.dumps([m for m in req.messages if m.get('role') == 'user'], default=str))
            return script(req)
        return wrapped

    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    chose = evaluate(tmp_path, watch(spawning_root()), ['constraints-release'], arm='adaptive', ledger=ledger)
    case = chose['cases'][0]
    assert case['outcome'] == 'passed', case
    assert case['delegation']['children'] == 1 and case['delegation']['spawn_denied'] == 0
    assert case['delegation']['attributable'] == 1
    assert chose['summary']['adaptive'] == {'cases_with_children': 1, 'cases_with_attributable_children': 1,
                                            'cases_observed': 1}
    assert 'harness delegation policy' in chose['policy']['adaptive']
    assert prompts and not any('Evaluation arm' in text for text in prompts)
    alone = evaluate(tmp_path, per_task(), ['constraints-release', 'memory-paraphrase'], arm='adaptive', ledger=ledger)
    assert [c['outcome'] for c in alone['cases']] == ['passed', 'passed']  # never non_delegated or unsupported
    assert alone['summary']['adaptive']['cases_with_children'] == 0
    solo = evaluate(tmp_path, spawning_root(), ['constraints-release'], ledger=ledger)
    assert solo['cases'][0]['delegation']['spawn_denied'] >= 1 and solo['cases'][0]['delegation']['children'] == 0
    assert 'adaptive' not in solo['policy'] and 'adaptive' not in solo['summary']
    assert set(solo['policy']) == {'permission_mode', 'denied_groups', 'approvals', 'answers', 'solo_spawn',
                                   'delegation', 'code_grading'}


def test_unknown_corpus_and_arm_refuse_before_any_scratch(tmp_path):
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    for kwargs, code in (({'corpus': '15a.9'}, 'corpus_unknown'), ({'corpus': None}, 'corpus_unknown'),
                         ({'corpus': '15a.1', 'case_ids': ['code-median']}, 'case_unknown'),
                         ({'arm': 'swarm'}, 'arm_invalid')):
        with pytest.raises(ev.EvaluationError, match=code):
            ev.run_evaluation(**{'mode': 'scripted', 'arm': 'adaptive', 'script': lambda req: final('x'),
                                 'scratch_root': scratch, **kwargs})
    assert list(scratch.iterdir()) == []
    profile = {'id': 'chatgpt-fixture', 'kind': 'chatgpt', 'url': 'https://api.openai.com/v1', 'model': 'gpt-6-luna',
               'context': 32768, 'client_id': 'oaiapp_fixture', 'subject': 'user-fixture',
               'connection_id': 'chatgpt-fixture'}
    with pytest.raises(ev.EvaluationError, match='hook_invalid'):
        ev.run_evaluation(mode='live', arm='adaptive', corpus='15a.1', profile=profile, prepare_app='invalid')
    with pytest.raises(ev.EvaluationError, match='live_mode_rejects_scripts'):
        ev.run_evaluation(mode='live', arm='adaptive', corpus='15a.1', profile=profile,
                          script=ev.minimal_path_script('adaptive', '15a.1'))


# -- comparator: same-revision pairs, cross-revision labels and refusals -------------------

def refused(compare, first, second, ledger, reason):
    with pytest.raises(ev.EvidenceRefused) as caught:
        compare(first, second, ledger)
    assert reason in caught.value.reasons, caught.value.reasons


def resign(ledger, report, **changes):
    body = {k: v for k, v in report.items() if k != 'signature'}
    return ledger.sign({**body, 'evaluation_id': uuid.uuid4().hex, **changes})


def test_solo_adaptive_pairs_compare_and_mismatches_are_refused(tmp_path):
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    cases = ['memory-paraphrase', 'stale-quota']
    wrong = {'stale-quota': 'stale', 'memory-paraphrase': 'near_name'}
    solo = evaluate(tmp_path, per_task(), cases, ledger=ledger)
    adaptive = evaluate(tmp_path, lambda req: transcript(req.case_id, wrong[req.case_id])(req), cases,
                        arm='adaptive', ledger=ledger)
    result = ev.compare_reports(solo, adaptive, ledger)
    assert result['mode'] == 'paired_same_revision' and result['pair'] == ['solo', 'adaptive']
    assert result['paired_outcomes'] == [{'case_id': 'memory-paraphrase', 'solo': 'passed', 'adaptive': 'failed'},
                                         {'case_id': 'stale-quota', 'solo': 'passed', 'adaptive': 'failed'}]
    assert result['agreement'] == {'both_passed': 0, 'solo_only': 2, 'adaptive_only': 0, 'neither': 0}
    assert set(result['arms']) == {'solo', 'adaptive'} and result['corpus']['version'] == '15a.1'
    assert ev.compare_reports(adaptive, solo, ledger)['agreement'] == result['agreement']

    delegated = evaluate(tmp_path, per_task(), cases, arm='delegated', ledger=ledger)
    refused(ev.compare_reports, delegated, adaptive, ledger, 'arms_must_be_solo_and_adaptive')
    refused(ev.compare_reports, adaptive, adaptive, ledger, 'same_evaluation_twice')
    refused(ev.compare_reports, adaptive, resign(ledger, adaptive), ledger, 'arms_must_be_solo_and_adaptive')
    legacy = ev.compare_reports(solo, delegated, ledger)  # historical pair keeps its fields
    assert legacy['pair'] == ['solo', 'delegated'] and set(legacy['agreement']) == {
        'both_passed', 'solo_only', 'delegated_only', 'neither'}
    assert legacy['unmatched'] == [{'case_id': 'memory-paraphrase', 'reason': 'delegated:unsupported'}]

    tighter = evaluate(tmp_path, per_task(), cases, arm='adaptive', ledger=ledger,
                       limits={'max_seconds': 30, 'max_steps': 10})
    refused(ev.compare_reports, solo, tighter, ledger, 'incomparable:limits')
    slower = evaluate(tmp_path, per_task(), cases, arm='adaptive', ledger=ledger, limits={'max_seconds': 40})
    refused(ev.compare_reports, solo, slower, ledger, 'incomparable:limits')
    other_corpus = ev.run_evaluation(mode='scripted', arm='adaptive', script=lambda req: final('FINAL: 7421'),
                                     case_ids=['memory-heron'], limits={'max_seconds': 30}, ledger=ledger,
                                     scratch_root=tmp_path, prepare_app=stub_guard)
    refused(ev.compare_reports, solo, other_corpus, ledger, 'incomparable:corpus')
    rerouted = resign(ledger, adaptive, route={**adaptive['route'], 'fingerprint': 'f' * 64})
    refused(ev.compare_reports, solo, rerouted, ledger, 'incomparable:route')
    revised = resign(ledger, adaptive, source={**adaptive['source'], 'sha256': 'e' * 64})
    refused(ev.compare_reports, solo, revised, ledger, 'incomparable:source')


def test_cross_revision_listing_is_labelled_and_never_a_paired_improvement(tmp_path, stable_source):
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    cases = ['memory-paraphrase', 'memory-conflict']
    baseline = evaluate(tmp_path, per_task('near_name'), cases, arm='adaptive', ledger=ledger)
    stable_source['value'] = {**stable_source['value'], 'sha256': 'b' * 64}  # a later harness revision
    candidate = evaluate(tmp_path, per_task(), cases, arm='adaptive', ledger=ledger)
    assert baseline['source']['stable'] and candidate['source']['stable']
    assert baseline['source']['sha256'] != candidate['source']['sha256']

    result = ev.compare_revisions(baseline, candidate, ledger)
    assert result['schema'] == ev.REVISION_SCHEMA != ev.COMPARISON_SCHEMA
    assert result['mode'] == 'cross_revision_descriptive' and result['paired_same_revision'] is False
    assert result['claim'] == 'none' and 'not a same-revision paired comparison' in result['label']
    assert result['inference'].startswith('Not a paired improvement')
    assert not {'agreement', 'paired_outcomes', 'arms', 'pair'} & set(result)
    keys = json.dumps(sorted(result)) + json.dumps(sorted(result['totals']['baseline']))
    assert not any(word in keys.lower() for word in ('improv', 'better', 'worse', 'win', 'gain'))
    assert result['case_outcomes'] == [{'case_id': 'memory-conflict', 'baseline': 'failed', 'candidate': 'passed'},
                                       {'case_id': 'memory-paraphrase', 'baseline': 'failed', 'candidate': 'passed'}]
    assert result['sources']['baseline']['sha256'] == baseline['source']['sha256']
    assert result['sources']['candidate']['sha256'] == 'b' * 64
    # The listing is not a signed evaluation and is never accepted as one.
    for compare in (ev.compare_reports, ev.compare_revisions):
        refused(compare, result, candidate, ledger, 'schema_mismatch')
    refused(ev.compare_reports, baseline, candidate, ledger, 'arms_must_be_solo_and_adaptive')

    refused(ev.compare_revisions, baseline, resign(ledger, baseline), ledger, 'same_source_use_paired_comparison')
    refused(ev.compare_revisions, baseline, baseline, ledger, 'same_evaluation_twice')
    other_arm = evaluate(tmp_path, per_task(), cases, ledger=ledger)
    refused(ev.compare_revisions, baseline, other_arm, ledger, 'arms_must_match')
    refused(ev.compare_revisions, baseline, resign(ledger, candidate, limits={**candidate['limits'], 'max_steps': 9}),
            ledger, 'incomparable:limits')
    refused(ev.compare_revisions, baseline, resign(ledger, candidate, evidence='live_model'), ledger,
            'incomparable:evidence')
    refused(ev.compare_revisions, baseline, resign(ledger, candidate, route={**candidate['route'], 'model': 'other'}),
            ledger, 'incomparable:route')
    refused(ev.compare_revisions, baseline, resign(ledger, candidate, corpus={**candidate['corpus'], 'fingerprint': '0' * 64}),
            ledger, 'incomparable:corpus')
    refused(ev.compare_revisions, baseline, resign(ledger, candidate, source={**candidate['source'], 'stable': False}),
            ledger, 'source_drift')
    tampered = json.loads(json.dumps(candidate))
    tampered['summary']['passed'] += 1
    refused(ev.compare_revisions, baseline, tampered, ledger, 'tampered_evidence')


# -- scripted economics: minimal paths per arm ----------------------------------------------



def _check_economics(result, corpus, steps=12):
    assert result['schema'] == ev.ECONOMICS_SCHEMA and result['evidence'] == 'scripted_fixture'
    assert result['quality_claim'].startswith('none') and result['token_estimator']['name'] == ev.TOKEN_ESTIMATOR
    assert result['source']['stable'] is True and result['guard'] == 'caller_prepared'
    for ident, row in result['cases'].items():
        root_only, forced = ev.MINIMAL_CALLS[corpus][ident]
        for arm in ('solo', 'adaptive'):
            assert row[arm]['outcome'] == 'passed', (ident, arm, row[arm])
            assert (row[arm]['root_steps'], row[arm]['child_calls']) == root_only, (ident, arm, row[arm])
            assert row[arm]['children'] == 0 and row[arm]['estimated_tokens']['total'] > 0
        if forced is None:
            assert row['delegated'] == {'outcome': 'unsupported'} and ident not in result['forced_delegation']
            continue
        assert row['delegated']['outcome'] == 'passed', (ident, row['delegated'])
        assert (row['delegated']['root_steps'], row['delegated']['child_calls']) == forced, (ident, row['delegated'])
        assert row['delegated']['attributable_children'] == 1
        item = result['forced_delegation'][ident]
        assert item['fits_max_steps'] and item['extra_steps'] == sum(forced) - sum(root_only) > 0
        assert item['delegated_shared_steps'] <= steps and item['estimated_token_ratio'] > 1
    assert result['conclusion']['did_not_fit'] == [] and result['conclusion']['max_steps'] == steps


def test_scripted_economics_10a2_shows_forced_delegation_fits_twelve_steps(tmp_path):
    result = ev.scripted_economics(corpus='10a.2', limits={'max_seconds': 30}, scratch_root=tmp_path,
                                   prepare_app=stub_guard)
    _check_economics(result, '10a.2')
    assert result['conclusion']['fit'] == ['code-median', 'files-orders', 'hostile-maintenance', 'research-ferries']
    serialized = json.dumps(result)
    assert 'Kestrel' not in serialized and 'FINAL' not in serialized and str(tmp_path) not in serialized
    assert not list(tmp_path.glob('pebble-eval-*'))


def test_scripted_economics_15a1_covers_every_task_and_arm(tmp_path):
    result = ev.scripted_economics(corpus='15a.1', limits={'max_seconds': 30}, scratch_root=tmp_path,
                                   prepare_app=stub_guard)
    _check_economics(result, '15a.1')
    assert result['conclusion']['delegable_cases'] == 4 and set(result['cases']) == set(ec.BY_ID)


def test_forced_delegation_threshold_is_eight_shared_steps_for_a_write(tmp_path):
    result = ev.scripted_economics(corpus='10a.2', case_ids=['code-median', 'research-ferries'],
                                   arms=('solo', 'delegated'), limits={'max_steps': 7, 'max_seconds': 30},
                                   scratch_root=tmp_path, prepare_app=stub_guard)
    item = result['forced_delegation']['code-median']
    assert item['solo_passed'] and not item['delegated_passed'] and not item['fits_max_steps']
    assert result['cases']['code-median']['delegated']['outcome'] == 'budget_exhausted'
    assert result['conclusion']['did_not_fit'] == ['code-median'] and result['conclusion']['fit'] == ['research-ferries']
    eight = ev.scripted_economics(corpus='10a.2', case_ids=['code-median'], arms=('delegated',),
                                  limits={'max_steps': 8, 'max_seconds': 30}, scratch_root=tmp_path,
                                  prepare_app=stub_guard)
    assert eight['cases']['code-median']['delegated']['outcome'] == 'passed'
    assert eight['cases']['code-median']['delegated']['slack_steps'] == 0
    assert eight['forced_delegation'] == {} and eight['conclusion']['delegable_cases'] == 0  # needs a solo arm


def test_economics_refuses_bad_arguments_and_the_estimator_is_deterministic(tmp_path):
    for kwargs, code in (({'arms': ('solo', 'swarm')}, 'arms_invalid'), ({'arms': 'solo'}, 'arms_invalid'),
                         ({'arms': ()}, 'arms_invalid'), ({'corpus': '15a.9'}, 'corpus_unknown'),
                         ({'limits': {'max_steps': 99}}, 'limits_out_of_bounds'), ({'prepare_app': 'x'}, 'hook_invalid')):
        with pytest.raises(ev.EvaluationError, match=code):
            ev.scripted_economics(scratch_root=tmp_path, **kwargs)
    assert list(tmp_path.iterdir()) == []
    assert ev.estimate_tokens({'a': 'bcd'}) == ev.estimate_tokens({'a': 'bcd'}) == 3  # 11 bytes -> 3
    assert ev.estimate_tokens('é') == 1 and ev.estimate_tokens([]) == 1


# -- dry-run live plan ------------------------------------------------------------------------

def test_live_plan_is_deterministic_and_lists_exactly_the_needed_runs(tmp_path):
    before = sorted(tmp_path.iterdir())
    pilot = ev.live_plan('pilot')
    assert pilot == ev.live_plan('pilot') and pilot['plan_sha256'] == ev.live_plan('pilot')['plan_sha256']
    assert pilot['schema'] == ev.PLAN_SCHEMA and pilot['performs_requests'] is False
    assert pilot['totals'] == {'invocations': 6, 'case_runs': 32, 'unsupported_listed': 4,
                               'minimum_model_calls': pilot['totals']['minimum_model_calls'],
                               'maximum_completed_model_calls': 32 * 12, 'maximum_admission_attempts': 32 * 36,
                               'maximum_wall_seconds': 32 * 240}
    minimum = sum(sum(ev.MINIMAL_CALLS[corpus][case][1 if arm == 'delegated' else 0])
                  for corpus in ev.CORPORA for arm in ev.ARMS for case in ev.MINIMAL_CALLS[corpus]
                  if ev.MINIMAL_CALLS[corpus][case][1 if arm == 'delegated' else 0])
    assert pilot['totals']['minimum_model_calls'] == minimum
    assert [(i['corpus'], i['arm'], i['repetition'], i['case_runs']) for i in pilot['invocations']] == [
        ('10a.2', 'solo', 1, 6), ('10a.2', 'delegated', 1, 4), ('10a.2', 'adaptive', 1, 6),
        ('15a.1', 'solo', 1, 6), ('15a.1', 'delegated', 1, 4), ('15a.1', 'adaptive', 1, 6)]
    delegated = pilot['invocations'][4]
    assert delegated['unsupported'] == ['memory-paraphrase', 'memory-conflict']
    assert all(row['requires_live_model'] for row in delegated['cases'])
    for invocation in pilot['invocations']:
        command = invocation['command']
        assert command[:3] == ['python', 'scripts/evaluate_harness.py', 'live'] and command[-1] == '--confirm-live'
        assert command[command.index('--ledger') + 1] == '<PRIVATE_LEDGER_DIR>'
        assert command[command.index('--profile-metadata') + 1] == '<PRIVATE_PROFILE_METADATA>'
        assert command[command.index('--corpus') + 1] == invocation['corpus']
    assert [c['pair'] for c in pilot['comparisons']] == [['solo', 'delegated'], ['solo', 'adaptive']] * 2
    assert all(c['model_calls'] == 0 for c in pilot['comparisons'])
    claim = ev.live_plan('claim')
    assert claim['totals']['invocations'] == 18 and claim['totals']['case_runs'] == 96
    assert claim['plan_sha256'] != pilot['plan_sha256'] and len(claim['comparisons']) == 12
    serialized = json.dumps(claim)
    assert str(tmp_path) not in serialized and '@' not in serialized and 'oaiapp_' not in serialized
    assert sorted(tmp_path.iterdir()) == before  # no ledger, report or scratch was created


def test_live_plan_options_flags_and_refusals():
    narrow = ev.live_plan('pilot', corpora=['15a.1'], arms=['adaptive', 'solo'], repetitions=2,
                          limits={'max_steps': 6, 'max_seconds': 60})
    assert narrow['arms'] == ['solo', 'adaptive'] and narrow['totals']['invocations'] == 4
    assert [c['pair'] for c in narrow['comparisons']] == [['solo', 'adaptive']] * 2
    assert narrow['corpora']['15a.1']['limits']['max_steps'] == 6
    tight = ev.live_plan('pilot', corpora=['10a.2'], arms=['delegated'], limits={'max_steps': 5})
    flagged = {row['case_id'] for row in tight['invocations'][0]['cases'] if row['minimum_exceeds_max_steps']}
    assert flagged == {'code-median', 'files-orders', 'research-ferries', 'hostile-maintenance'}
    for kwargs, code in (({'claim_set': 'everything'}, 'claim_set_unknown'), ({'repetitions': 0}, 'repetitions_invalid'),
                         ({'repetitions': 6}, 'repetitions_invalid'), ({'repetitions': True}, 'repetitions_invalid'),
                         ({'arms': ['swarm']}, 'arms_invalid'), ({'arms': 'solo'}, 'arms_invalid'),
                         ({'corpora': ['9z']}, 'corpora_invalid'), ({'corpora': []}, 'corpora_invalid'),
                         ({'limits': {'wall_seconds': 10}}, 'limits_invalid'),
                         ({'limits': {'max_steps': 40}}, 'limits_out_of_bounds')):
        with pytest.raises(ev.EvaluationError, match=code):
            ev.live_plan(**{'claim_set': 'pilot', **kwargs})


# -- CLI ------------------------------------------------------------------------------------

def test_cli_plan_corpus_economics_and_refusals(tmp_path, capsys):
    cli = load_cli()
    assert cli.main(['plan', '--claim-set', 'pilot']) == 0
    assert json.loads(capsys.readouterr().out) == ev.live_plan('pilot')
    out = tmp_path / 'plan.json'
    assert cli.main(['plan', '--corpora', '15a.1', '--arms', 'solo,adaptive', '--report', str(out)]) == 0
    capsys.readouterr()
    assert json.loads(out.read_text())['arms'] == ['solo', 'adaptive']
    assert cli.main(['plan', '--arms', 'swarm']) == 2
    claim = ev.live_plan('claim')
    for invocation in claim['invocations']:  # every planned command parses as written
        parsed = cli._parser().parse_args(invocation['command'][2:])
        limits = claim['corpora'][invocation['corpus']]['limits']
        assert (parsed.command, parsed.arm, parsed.corpus, parsed.confirm_live) == (
            'live', invocation['arm'], invocation['corpus'], True)
        assert (parsed.max_steps, parsed.max_seconds, parsed.wall_seconds, parsed.total_wall_seconds) == (
            limits['max_steps'], limits['max_seconds'], limits['wall_seconds'], limits['total_wall_seconds'])
    assert cli.main(['corpus', '--corpus', '15a.1']) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest['version'] == '15a.1' and manifest == ev.corpus_manifest('15a.1')

    metadata = tmp_path / 'selected.json'
    metadata.write_text(json.dumps({'profile': {'id': 'x'}}))
    os.chmod(metadata, 0o600)
    report = tmp_path / 'live.json'
    assert cli.main(['live', '--profile-metadata', str(metadata), '--arm', 'adaptive', '--corpus', '15a.1',
                     '--ledger', str(tmp_path / 'ledger'), '--report', str(report)]) == 2
    assert not report.exists() and not (tmp_path / 'ledger').exists()
    forged = tmp_path / 'forged.json'
    forged.write_text(json.dumps({'schema': ev.SCHEMA, 'evaluation_id': uuid.uuid4().hex, 'signature': None}))
    assert cli.main(['compare-revisions', str(forged), str(forged), '--ledger', str(tmp_path / 'ledger')]) == 2

    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    assert cli.main(['economics', '--corpus', '15a.1', '--cases', 'memory-conflict', '--arms', 'solo,adaptive',
                     '--max-seconds', '30', '--scratch', str(scratch)]) == 0
    economics = json.loads(capsys.readouterr().out)
    assert economics['guard'] == 'fixture_allow' and economics['evidence'] == 'scripted_fixture'
    assert economics['cases']['memory-conflict']['adaptive']['outcome'] == 'passed'
    assert list(scratch.iterdir()) == []
